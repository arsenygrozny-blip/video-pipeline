import asyncio
import base64
import hashlib
import json
import os
import re
import tempfile
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

# ============================ НАСТРОЙКИ (через переменные окружения Render) ============================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0") or 0)
PORT = int(os.environ.get("PORT", "10000"))
BASE_URL = (os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_BASE_URL") or "").rstrip("/")
KAGGLE_USERNAME = os.environ.get("KAGGLE_USERNAME", "")
COOKIES_TXT = os.environ.get("COOKIES_TXT", "")
KERNEL_SLUG = os.environ.get("KERNEL_SLUG", "video-worker")
RENDER_API_KEY = os.environ.get("RENDER_API_KEY", "")
RENDER_SERVICE_ID = os.environ.get("RENDER_SERVICE_ID", "srv-dbac39mq1p3s73b43ns0")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "arsenygrozny-blip/video-pipeline")
QUEUE_BRANCH = "bot-data"
QUEUE_FILE = ".queue.json"
BG_FILE = ".backgrounds.json"
BG_PREVIEWS_DIR = "bg_previews"
# =======================================================================================================

SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
URL_PATH = hashlib.sha256((BOT_TOKEN + "path").encode()).hexdigest()[:24]
WORKER_FILE = Path(__file__).parent / "worker.py"
if not WORKER_FILE.exists():
    raise SystemExit("Рядом с main.py должен лежать worker.py — это код, который запустится на Kaggle")
WORKER_SRC = WORKER_FILE.read_text(encoding="utf-8")
URL_RE = re.compile(r"https?://\S+")
YT_RE = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})")

STATE = {"banner": os.environ.get("BANNER_FILE_ID", "")}

MENU = ReplyKeyboardMarkup(
    [["🎬 Новое видео", "📊 Квоты"], ["📋 Очередь", "📚 Мои фоны"]],
    resize_keyboard=True,
)
CANCEL = ("❌ Отмена", "cancel")

# ---------------------------- очереди и базы ----------------------------
QUEUE = []          # [{"job": {...}, "chat_id": int}]
BACKGROUNDS = []    # [{"id", "url", "name", "end_time", "preview_id", "added_at"}]
PROCESSING = False
RUNNING = False

STYLES = [
    ("classic", "Классика"),
    ("shorts", "Shorts"),
    ("neon", "Неон"),
    ("lime", "Лайм"),
    ("minimal", "Минимал"),
]
STYLE_LABELS = dict(STYLES)
DEFAULT_STYLE = (os.environ.get("STYLE", "classic") or "classic").strip().lower()
if DEFAULT_STYLE not in STYLE_LABELS:
    DEFAULT_STYLE = "classic"

MAX_UPLOAD = 20 * 1024 * 1024

STEP_ORDER = ["audio", "bgs", "banner", "cover", "ready"]


class Busy(Exception):
    pass


def kb(*rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=c) for t, c in row] for row in rows])


def file_obj(msg):
    for attr in ("audio", "voice", "video", "video_note", "animation", "document"):
        obj = getattr(msg, attr, None)
        if obj is not None:
            return obj
    return None


# ---------------------------- GitHub helpers ----------------------------
def _gh_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "video-bot",
    }


def _gh_get(path, params=None):
    r = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/{path}",
        headers=_gh_headers(), params=params, timeout=15)
    return r


def _gh_put(path, body):
    return requests.put(
        f"https://api.github.com/repos/{GITHUB_REPO}/{path}",
        headers=_gh_headers(), json=body, timeout=15)


def _gh_delete(path):
    return requests.delete(
        f"https://api.github.com/repos/{GITHUB_REPO}/{path}",
        headers=_gh_headers(), timeout=15)


def _ensure_branch():
    """Создаёт ветку bot-data от main, если её нет."""
    r = _gh_get(f"git/ref/heads/{QUEUE_BRANCH}")
    if r.ok:
        return True
    r2 = _gh_get("git/ref/heads/main")
    if not r2.ok:
        return False
    main_sha = r2.json()["object"]["sha"]
    r3 = requests.post(
        f"https://api.github.com/repos/{GITHUB_REPO}/git/refs",
        headers=_gh_headers(),
        json={"ref": f"refs/heads/{QUEUE_BRANCH}", "sha": main_sha},
        timeout=15)
    return r3.ok or r3.status_code == 422


def _gh_file_sha(path):
    r = _gh_get(f"contents/{path}?ref={QUEUE_BRANCH}")
    if r.ok:
        return r.json().get("sha")
    return None


def _gh_write_file(path, data_bytes, message):
    """Записывает/перезаписывает файл на ветке bot-data."""
    if not GITHUB_TOKEN:
        return False
    _ensure_branch()
    sha = _gh_file_sha(path)
    content = base64.b64encode(data_bytes).decode()
    body = {"message": message, "content": content, "branch": QUEUE_BRANCH}
    if sha:
        body["sha"] = sha
    r = _gh_put(f"contents/{path}", body)
    return r.status_code in (200, 201)


def _gh_read_file(path):
    if not GITHUB_TOKEN:
        return None
    _ensure_branch()
    r = _gh_get(f"contents/{path}?ref={QUEUE_BRANCH}")
    if not r.ok:
        return None
    try:
        return base64.b64decode(r.json()["content"]).decode()
    except Exception:
        return None


def _gh_delete_file(path):
    if not GITHUB_TOKEN:
        return False
    _ensure_branch()
    sha = _gh_file_sha(path)
    if not sha:
        return True
    body = {"message": f"delete {path}", "sha": sha, "branch": QUEUE_BRANCH}
    r = _gh_delete(f"contents/{path}")
    return r.status_code in (200, 204)


# ---------------------------- очередь ----------------------------
def queue_save():
    if not GITHUB_TOKEN:
        return
    try:
        data = json.dumps({"queue": QUEUE, "running": RUNNING}, ensure_ascii=False)
        _gh_write_file(QUEUE_FILE, data.encode(), f"queue update {len(QUEUE)}")
    except Exception as e:
        print("queue_save error:", e, flush=True)


def queue_load():
    global QUEUE, RUNNING
    if not GITHUB_TOKEN:
        return
    try:
        raw = _gh_read_file(QUEUE_FILE)
        if raw is None:
            return
        data = json.loads(raw)
        QUEUE[:] = data.get("queue", [])
        RUNNING = data.get("running", False)
        print(f"queue_load: {len(QUEUE)} заданий, running={RUNNING}", flush=True)
    except Exception as e:
        print("queue_load error:", e, flush=True)


# ---------------------------- база фонов ----------------------------
def backgrounds_save():
    if not GITHUB_TOKEN:
        return
    try:
        data = json.dumps({"backgrounds": BACKGROUNDS}, ensure_ascii=False, indent=2)
        _gh_write_file(BG_FILE, data.encode(), f"backgrounds update {len(BACKGROUNDS)}")
    except Exception as e:
        print("backgrounds_save error:", e, flush=True)


def backgrounds_load():
    global BACKGROUNDS
    if not GITHUB_TOKEN:
        return
    try:
        raw = _gh_read_file(BG_FILE)
        if raw is None:
            print("backgrounds_load: файла нет", flush=True)
            return
        data = json.loads(raw)
        BACKGROUNDS[:] = data.get("backgrounds", [])
        print(f"backgrounds_load: {len(BACKGROUNDS)} фонов", flush=True)
    except Exception as e:
        print("backgrounds_load error:", e, flush=True)


def backgrounds_preview_path(bg_id):
    return f"{BG_PREVIEWS_DIR}/{bg_id}.jpg"


def backgrounds_save_preview(bg_id, image_bytes):
    if not GITHUB_TOKEN:
        return False
    return _gh_write_file(backgrounds_preview_path(bg_id), image_bytes, f"preview {bg_id}")


def backgrounds_delete_preview(bg_id):
    if not GITHUB_TOKEN:
        return
    _gh_delete_file(backgrounds_preview_path(bg_id))


def backgrounds_add(url, name, end_time=None, preview_bytes=None):
    bg_id = "bg_" + uuid.uuid4().hex[:12]
    if preview_bytes:
        backgrounds_save_preview(bg_id, preview_bytes)
    BACKGROUNDS.append({
        "id": bg_id,
        "url": url,
        "name": name or url,
        "end_time": end_time,
        "preview_id": bg_id if preview_bytes else None,
        "added_at": datetime.now(timezone.utc).isoformat(),
    })
    backgrounds_save()
    return bg_id


def backgrounds_remove(bg_id):
    global BACKGROUNDS
    BACKGROUNDS = [b for b in BACKGROUNDS if b["id"] != bg_id]
    backgrounds_delete_preview(bg_id)
    backgrounds_save()


def find_background(bg_id):
    for b in BACKGROUNDS:
        if b["id"] == bg_id:
            return b
    return None


# ---------------------------- превью фона ----------------------------
def _youtube_video_id(url):
    m = YT_RE.search(url)
    return m.group(1) if m else None


def extract_preview(url):
    """Возвращает (image_bytes или None, превью_url или None)."""
    yt_id = _youtube_video_id(url)
    if yt_id:
        # Пробуем maxresdefault, потом hqdefault
        for fmt in ("maxresdefault", "hqdefault", "mqdefault"):
            preview_url = f"https://img.youtube.com/vi/{yt_id}/{fmt}.jpg"
            try:
                r = requests.get(preview_url, timeout=15)
                if r.ok and r.content and len(r.content) > 1000:
                    return r.content, preview_url
            except Exception:
                pass
        return None, f"https://img.youtube.com/vi/{yt_id}/hqdefault.jpg"
    # Для не-Youtube: скачиваем первый кадр через ffmpeg
    try:
        tmp = Path(tempfile.mkdtemp()) / "preview.mp4"
        # Пробуем скачать через yt-dlp
        import yt_dlp
        ydl_opts = {
            "outtmpl": str(tmp),
            "quiet": True,
            "noplaylist": True,
            "format": "worst",
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        # Берём первый кадр
        out_img = Path(tempfile.mkdtemp()) / "frame.jpg"
        import subprocess
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(tmp), "-vframes", "1", str(out_img)],
            capture_output=True, timeout=60)
        if out_img.exists():
            return out_img.read_bytes(), None
    except Exception as e:
        print("preview extract error:", e, flush=True)
    return None, None


# ---------------------------- запуск Kaggle ----------------------------
def kaggle_api():
    from kaggle.api.kaggle_api_extended import KaggleApi

    if not os.environ.get("KAGGLE_USERNAME") or not os.environ.get("KAGGLE_KEY"):
        raise RuntimeError("На Render не заданы KAGGLE_USERNAME и/или KAGGLE_KEY.")
    api = KaggleApi()
    try:
        api.authenticate()
    except Exception as e:
        raise RuntimeError(f"Kaggle не принял ключи: {e}") from e
    return api


def kaggle_run(job, max_retries=4):
    """Запуск на Kaggle с повтором при Busy (вариант А)."""
    api = kaggle_api()
    slug = f"{KAGGLE_USERNAME}/{KERNEL_SLUG}"

    last_err = None
    for attempt in range(max_retries):
        try:
            st = api.kernels_status(slug)
            status = str(getattr(st, "status", st)).lower()
            if "running" in status or "queued" in status:
                raise Busy()
        except Busy:
            last_err = "Kaggle занят"
            if attempt < max_retries - 1:
                import asyncio
                import time
                time.sleep(15 * (attempt + 1))
                continue
            raise
        except Exception:
            pass  # ядра ещё нет, первый запуск
        break

    d = Path(tempfile.mkdtemp())
    script = "import json\nJOB = json.loads(" + repr(json.dumps(job, ensure_ascii=False)) + ")\n" + WORKER_SRC
    (d / "worker_job.py").write_text(script, encoding="utf-8")
    meta = {
        "id": slug,
        "title": "Video Worker",
        "code_file": "worker_job.py",
        "language": "python",
        "kernel_type": "script",
        "is_private": True,
        "enable_gpu": True,
        "enable_internet": True,
        "dataset_sources": [],
        "competition_sources": [],
        "kernel_sources": [],
    }
    (d / "kernel-metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    res = api.kernels_push(str(d))
    err = getattr(res, "error", None)
    if err:
        raise RuntimeError(f"Kaggle отклонил запуск: {err}")


def kaggle_prune():
    try:
        r = requests.delete(
            f"https://www.kaggle.com/api/v1/kernels/{KAGGLE_USERNAME}/{KERNEL_SLUG}/output",
            auth=(KAGGLE_USERNAME, os.environ.get("KAGGLE_KEY", "")),
            timeout=15)
        print(f"kaggle_prune: {r.status_code}", flush=True)
    except Exception as e:
        print("kaggle_prune:", e, flush=True)


def kaggle_cancel():
    try:
        api = kaggle_api()
    except Exception as e:
        print("Kaggle cancel: нет доступа:", e)
        return False
    slug = f"{KAGGLE_USERNAME}/{KERNEL_SLUG}"
    for name in ("kernels_cancel", "kernel_session_cancel", "kernels_session_cancel", "cancel_kernel_session"):
        fn = getattr(api, name, None)
        if fn:
            try:
                fn(slug)
                print("Kaggle cancel OK via", name)
                return True
            except Exception as e:
                print("Kaggle cancel", name, "failed:", e)
    return False


# ---------------------------- диалог ----------------------------
async def guard(update: Update):
    uid = update.effective_user.id
    if ALLOWED_USER_ID == 0:
        await update.effective_message.reply_text(
            f"Твой ID: {uid}\nВпиши его в ALLOWED_USER_ID на Render.")
        return False
    return uid == ALLOWED_USER_ID


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text("Готов. Жми «🎬 Новое видео».", reply_markup=MENU)


def _track(context, msg):
    mid = getattr(msg, "message_id", None)
    if mid:
        context.user_data.setdefault("flow_msgs", []).append(mid)


async def cleanup_flow(chat_id, context, bot):
    for mid in context.user_data.pop("flow_msgs", []):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass


def _back_row(job):
    step = job.get("step", "audio")
    if step in ("bgs", "banner", "cover", "ready"):
        return [("◀️ Назад", "back")]
    return []


async def start_job(msg, context):
    await cleanup_flow(msg.chat_id, context, context.bot)
    context.user_data["job"] = {"id": uuid.uuid4().hex[:8], "step": "audio", "bgs": [],
                                "style": DEFAULT_STYLE}
    _track(context, msg)
    _track(context, await msg.reply_text(
        "1/5. Пришли озвучку — ссылкой или файлом (до 20 МБ).",
        reply_markup=kb([CANCEL])))


async def ask_bgs(msg, context):
    job = context.user_data["job"]
    rows = []
    if BACKGROUNDS:
        rows.append([("📚 Мои фоны", "bg_library")])
        rows.append([("🎲 Случайный фон", "bg_random")])
    rows += _back_row(job) + [("✅ Готово", "bgs_done"), CANCEL]
    _track(context, await msg.reply_text(
        "2/5. Пришли фоны — ссылками или файлами. Склеятся по порядку. Жми «Готово» когда хватит.",
        reply_markup=kb(rows)))


async def ask_banner(msg, context):
    job = context.user_data["job"]
    row = _back_row(job) + [("⏭ Пропустить", "skip_banner")]
    if STATE["banner"]:
        row.insert(1, ("♻️ Прошлый баннер", "use_banner"))
    row.append(CANCEL)
    _track(context, await msg.reply_text(
        "3/5. Пришли баннер файлом (mp4/gif/webm) или пропусти.",
        reply_markup=kb(row)))


async def ask_cover(msg, context):
    job = context.user_data["job"]
    rows = _back_row(job) + [("⏭ Пропустить", "skip_cover"), CANCEL]
    _track(context, await msg.reply_text(
        "4/5. Напиши текст обложки или пропусти.",
        reply_markup=kb(rows)))


def ready_text(job):
    cover = job.get("cover_text")
    style = job.get("style", DEFAULT_STYLE)
    return (f"Всё готово:\n• озвучка: есть\n• фонов: {len(job['bgs'])}\n"
            f"• баннер: {'да' if job.get('banner_file_id') else 'нет'}\n"
            f"• обложка: {('«' + cover[:60] + '»') if cover else 'нет'}\n"
            f"• стиль: {STYLE_LABELS.get(style, style)}\n\n"
            f"Добавь в очередь, потом запусти когда готов.")


def ready_kb(job):
    rows = [[(label, f"style:{key}") for key, label in STYLES[i:i + 2]]
            for i in range(0, len(STYLES), 2)]
    rows.append([("✅ В очередь", "create")])
    rows.append([("◀️ Назад", "back"), CANCEL])
    return kb(*rows)


async def show_ready(msg, job, context):
    _track(context, await msg.reply_text(ready_text(job), reply_markup=ready_kb(job)))


# ---------------------------- библиотека фонов ----------------------------
async def cmd_bg_library(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать сохранённые фоны с превью."""
    if not await guard(update):
        return
    if not BACKGROUNDS:
        await update.message.reply_text(
            "Библиотека фонов пуста.\nПришли ссылку на видео — и она сохранится как фон.",
            reply_markup=kb([("➕ Добавить фон", "bg_add")], [("◀️ Назад в меню", "menu_back")]))
        return
    lines = ["📚 Мои фоны:\n"]
    for b in BACKGROUNDS:
        lines.append(f"• {b['name']}\n  {b['url'][:60]}")
    text = "\n".join(lines)
    kb_markup = kb(
        [("➕ Добавить фон", "bg_add")],
        [("🗑 Удалить фон", "bg_delete")],
        [("◀️ Назад в меню", "menu_back")],
    )
    await update.message.reply_text(text, reply_markup=kb_markup)


async def cmd_bg_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начать добавление фона: просим ссылку."""
    if not await guard(update):
        return
    context.user_data["bg_flow"] = {"step": "url"}
    await update.message.reply_text(
        "Пришли ссылку на видео (YouTube или прямая ссылка).",
        reply_markup=kb([CANCEL]))


async def cmd_bg_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаление фона — показываем список с кнопками удалить."""
    if not await guard(update):
        return
    if not BACKGROUNDS:
        await update.message.reply_text("Библиотека пуста.")
        return
    rows = []
    for b in BACKGROUNDS:
        rows.append([(f"🗑 {b['name'][:30]}", f"bg_del_{b['id']}")])
    rows.append([("◀️ Назад", "bg_lib_back")])
    await update.message.reply_text("Выбери фон для удаления:", reply_markup=kb(*rows))


# ---------------------------- приём данных ----------------------------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    msg = update.message
    text = (msg.text or "").strip()

    # Фоновый поток добавления
    bg_flow = context.user_data.get("bg_flow")
    if bg_flow:
        if bg_flow["step"] == "url":
            urls = URL_RE.findall(text)
            if not urls:
                await msg.reply_text("Нужна ссылка (http…).")
                return
            url = urls[0]
            context.user_data["bg_flow"]["url"] = url
            context.user_data["bg_flow"]["step"] = "name"
            # Извлекаем превью
            preview_bytes, preview_url = extract_preview(url)
            if preview_bytes:
                context.user_data["bg_flow"]["preview_bytes"] = preview_bytes
                await msg.reply_photo(
                    photo=preview_bytes,
                    caption="Превью фонового видео. Пришли имя для фон (или «пропусти»).",
                    reply_markup=kb([("⏭ Пропустить", "bg_name_skip"), CANCEL]))
            else:
                await msg.reply_text(
                    "Превью не извлеклось (не YouTube и не скачалось).\n"
                    "Пришли имя для фон (или «пропусти»).",
                    reply_markup=kb([("⏭ Пропустить", "bg_name_skip"), CANCEL]))
            return
        elif bg_flow["step"] == "name":
            name = None if text == "пропусти" else text
            url = bg_flow["url"]
            preview_bytes = bg_flow.get("preview_bytes")
            end_time = None
            bg_id = backgrounds_add(url, name, end_time=end_time, preview_bytes=preview_bytes)
            context.user_data.pop("bg_flow", None)
            await msg.reply_text(
                f"✅ Фон сохранён: {name or url}\nID: {bg_id}",
                reply_markup=MENU)
            return

    if text == "🎬 Новое видео":
        await start_job(msg, context)
        return
    if text == "📊 Квоты":
        await cmd_quota(update, context)
        return
    if text == "📋 Очередь":
        await cmd_queue(update, context)
        return
    if text == "📚 Мои фоны":
        await cmd_bg_library(update, context)
        return
    job = context.user_data.get("job")
    if not job:
        await msg.reply_text("Нажми «🎬 Новое видео».", reply_markup=MENU)
        return
    _track(context, msg)
    step = job["step"]
    if step == "audio":
        urls = URL_RE.findall(text)
        if not urls:
            await msg.reply_text("Нужна ссылка (http…) или файлом.")
            return
        job["audio"] = urls[0]
        job["step"] = "bgs"
        await ask_bgs(msg, context)
    elif step == "bgs":
        urls = URL_RE.findall(text)
        if not urls:
            await msg.reply_text("Нужна ссылка или файл.")
            return
        job["bgs"] += urls
        _track(context, await msg.reply_text(
            f"Фонов: {len(job['bgs'])}. Ещё или «Готово».",
            reply_markup=kb(_back_row(job) + [("✅ Готово", "bgs_done"), CANCEL])))
    elif step == "cover":
        job["cover_text"] = text
        job["step"] = "ready"
        await show_ready(msg, job, context)
    elif step == "banner":
        await msg.reply_text("Нужен файл баннера или «Пропустить».")
    else:
        await msg.reply_text("Жми «✅ В очередь» или «Отмена».")


async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    msg = update.message
    job = context.user_data.get("job")
    obj = file_obj(msg)
    if not job:
        await msg.reply_text("Нажми «🎬 Новое видео».", reply_markup=MENU)
        return
    if obj is None:
        await msg.reply_text("Не понял формат. Пришли видео, гифку, аудио или документ.")
        return
    if obj.file_size and obj.file_size > MAX_UPLOAD:
        await msg.reply_text("Больше 20 МБ — Telegram не даёт ботам скачивать. Сожми или пришли ссылкой.")
        return
    step = job["step"]
    _track(context, msg)
    if step == "audio":
        job["audio"] = {"file_id": obj.file_id}
        job["step"] = "bgs"
        _track(context, await msg.reply_text("Озвучка принята."))
        await ask_bgs(msg, context)
    elif step == "bgs":
        job["bgs"].append({"file_id": obj.file_id})
        _track(context, await msg.reply_text(
            f"Фонов: {len(job['bgs'])}. Ещё или «Готово».",
            reply_markup=kb(_back_row(job) + [("✅ Готово", "bgs_done"), CANCEL])))
    elif step == "banner":
        STATE["banner"] = obj.file_id
        job["banner_file_id"] = obj.file_id
        job["step"] = "cover"
        _track(context, await msg.reply_text(
            f"Баннер принят. Для памяти после сна впиши BANNER_FILE_ID:\n{obj.file_id}"))
        await ask_cover(msg, context)
    else:
        await msg.reply_text("Сейчас файл не нужен.", reply_markup=MENU)


async def safe_edit(m, t, markup=None):
    try:
        await m.edit_text(t, reply_markup=markup)
    except Exception:
        pass


# ---------------------------- запуск на Kaggle ----------------------------
async def launch(chat_id, job, bot):
    status = await bot.send_message(chat_id, "🚀 Отправляю на Kaggle…")
    job = dict(job)
    job.update({
        "token": BOT_TOKEN,
        "chat_id": chat_id,
        "status_msg": status.message_id,
        "cookies": COOKIES_TXT,
    })
    job.pop("step", None)
    try:
        await asyncio.to_thread(kaggle_run, job)
        await safe_edit(status, "⏳ Задание ушло. Старт ~несколько минут, дальше статусы и видео.",
                        kb([("🛑 Отменить", "cancel_run")]))
        asyncio.get_event_loop().call_later(5, lambda: asyncio.ensure_future(
            asyncio.to_thread(kaggle_prune)))
    except Busy:
        await safe_edit(status, "⏳ Kaggle ещё занят предыдущим. Подожди и запусти заново.")
    except Exception as e:
        print("LAUNCH ERROR:", e, flush=True)
        await safe_edit(status, f"❌ Ошибка Kaggle:\n{str(e)[-700:]}")


# ---------------------------- обработчик очереди ----------------------------
async def queue_worker(app):
    global PROCESSING
    while True:
        await asyncio.sleep(8)
        if PROCESSING or not RUNNING or not QUEUE:
            continue
        PROCESSING = True
        item = QUEUE.pop(0)
        chat_id = item["chat_id"]
        job = item["job"]
        queue_save()
        try:
            n_left = len(QUEUE)
            prefix = f"📋 Осталось: {n_left}\n" if n_left else ""
            await app.bot.send_message(chat_id, prefix + "🎬 Запускаю видео…")
            await launch(chat_id, job, app.bot)
        except Exception as e:
            print("QUEUE ERROR:", e, flush=True)
            try:
                await app.bot.send_message(chat_id, f"❌ Ошибка: {str(e)[:200]}")
            except Exception:
                pass
        finally:
            PROCESSING = False
            queue_save()


def queue_snapshot():
    lines = []
    if PROCESSING:
        lines.append("⏳ Обрабатывается видео на Kaggle")
    status = "▶️ Обработка запущена" if RUNNING else "⏸ Пауза (нажми «▶️ Запустить»)"
    if QUEUE or PROCESSING:
        lines.append(status)
    for i, item in enumerate(QUEUE, 1):
        j = item["job"]
        style = j.get("style", DEFAULT_STYLE)
        n_bgs = len(j.get("bgs", []))
        cover = j.get("cover_text", "")
        cover_short = f", «{cover[:20]}»" if cover else ""
        lines.append(f"{i}. {STYLE_LABELS.get(style, style)}, фонов: {n_bgs}{cover_short}")
    if not lines:
        lines.append("Очередь пуста.")
    return "\n".join(lines)


def queue_kb():
    rows = []
    if QUEUE:
        if RUNNING:
            rows.append([("⏸ Пауза", "queue_pause")])
        else:
            rows.append([("▶️ Запустить", "queue_run")])
        rows.append([("🗑 Очистить", "queue_clear")])
    elif PROCESSING:
        rows.append([("🗑 Остановить", "queue_clear")])
    return kb(*rows) if rows else None


# ---------------------------- кнопки ----------------------------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global RUNNING
    q = update.callback_query
    if not await guard(update):
        return
    try:
        await q.answer()
    except Exception:
        pass
    data = q.data
    msg = q.message
    job = context.user_data.get("job")

    # Фоновый поток
    bg_flow = context.user_data.get("bg_flow")
    if bg_flow and data == "bg_name_skip":
        context.user_data["bg_flow"]["name"] = None
        context.user_data["bg_flow"]["step"] = "name"
        await msg.edit_caption("Пришли имя для фон (или «пропусти»).")
        return

    if data == "cancel_run":
        try:
            await msg.delete()
        except Exception:
            pass
        await context.bot.send_message(msg.chat_id, "🛑 Отмена отправлена.", reply_markup=MENU)
        await asyncio.to_thread(kaggle_cancel)
        return

    if data == "cancel":
        context.user_data.pop("job", None)
        context.user_data.pop("bg_flow", None)
        await cleanup_flow(msg.chat_id, context, context.bot)
        await msg.reply_text("Отменено.", reply_markup=MENU)
        return

    if data == "queue_clear":
        n = len(QUEUE)
        QUEUE.clear()
        RUNNING = False
        queue_save()
        await safe_edit(msg, f"🗑 Очищено ({n} заданий).")
        return

    if data == "queue_run":
        RUNNING = True
        queue_save()
        await safe_edit(msg, f"▶️ Запускаю очередь ({len(QUEUE)} видео)…", queue_kb())
        return

    if data == "queue_pause":
        RUNNING = False
        queue_save()
        await safe_edit(msg, "⏸ Пауза. Очередь сохранена.", queue_kb())
        return

    if data == "menu_back":
        await context.bot.send_message(msg.chat_id, "Главное меню:", reply_markup=MENU)
        try:
            await msg.delete()
        except Exception:
            pass
        return

    if data == "bg_lib_back":
        await cmd_bg_library(update, context)
        return

    if data == "bg_add":
        context.user_data["bg_flow"] = {"step": "url"}
        await safe_edit(msg, "Пришли ссылку на видео (YouTube или прямая ссылка).", kb([CANCEL]))
        return

    if data == "bg_delete":
        await cmd_bg_delete(update, context)
        return

    if data.startswith("bg_del_"):
        bg_id = data[len("bg_del_"):]
        backgrounds_remove(bg_id)
        await safe_edit(msg, f"🗑 Фон удалён.")
        await cmd_bg_delete(update, context)
        return

    if data == "bg_library":
        await cmd_bg_library(update, context)
        return

    if data == "bg_random":
        if not BACKGROUNDS:
            await msg.reply_text("Библиотека фонов пуста.")
            return
        import random
        bg = random.choice(BACKGROUNDS)
        job.setdefault("bgs", []).append({
            "url": bg["url"],
            "name": bg["name"],
            "source": "library",
            "bg_id": bg["id"],
            "end_time": bg.get("end_time"),
        })
        await safe_edit(msg, f"✅ Добавлен случайный фон: {bg['name']}")
        return

    if data == "cancel_run":
        return

    if data == "cancel":
        return

    if not job:
        await msg.reply_text("Это уже неактуально. Жми «🎬 Новое видео».", reply_markup=MENU)
        return

    if data.startswith("style:"):
        key = data.split(":", 1)[1]
        if key in STYLE_LABELS:
            job["style"] = key
        await safe_edit(msg, ready_text(job), ready_kb(job))
        return

    if data == "bgs_done":
        if not job["bgs"]:
            await msg.reply_text("Нужен хотя бы один фон.")
            return
        job["step"] = "banner"
        await ask_banner(msg, context)
    elif data == "skip_banner":
        job["step"] = "cover"
        await ask_cover(msg, context)
    elif data == "use_banner":
        job["banner_file_id"] = STATE["banner"]
        job["step"] = "cover"
        await ask_cover(msg, context)
    elif data == "skip_cover":
        job["step"] = "ready"
        await show_ready(msg, job, context)
    elif data == "create":
        context.user_data.pop("job", None)
        context.user_data.pop("bg_flow", None)
        await cleanup_flow(msg.chat_id, context, context.bot)
        QUEUE.append({"job": job, "chat_id": msg.chat_id})
        queue_save()
        pos = len(QUEUE)
        run_info = "\n▶️ Нажми «Запустить» в очереди, когда будешь готов." if not RUNNING else ""
        await context.bot.send_message(
            msg.chat_id,
            f"✅ В очереди: {pos} видео.{run_info}",
            reply_markup=MENU)


# ---------------------------- команды ----------------------------
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    def _check():
        api = kaggle_api()
        st = api.kernels_status(f"{KAGGLE_USERNAME}/{KERNEL_SLUG}")
        return str(getattr(st, "status", st))
    try:
        status = await asyncio.to_thread(_check)
        await update.message.reply_text(f"Kaggle: {status}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {str(e)[-400:]}")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"ID: {update.effective_user.id}")


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    text = "📋 Очередь:\n\n" + queue_snapshot()
    await update.message.reply_text(text, reply_markup=queue_kb())


# ---------------------------- квоты ----------------------------
def _fmt_hours(td):
    secs = int(round(td.total_seconds()))
    h, m = divmod(secs // 60, 60)
    return f"{h} ч {m:02d} мин" if h else f"{m} мин"


def _fmt_mb(mb):
    return f"{mb / 1024:.2f} ГБ" if mb >= 1024 else f"{mb:.1f} МБ"


def kaggle_quota_lines():
    q = kaggle_api().quota_view()
    lines = []
    for tag, attr in (("GPU", "gpu_quota"), ("TPU", "tpu_quota")):
        acc = getattr(q, attr, None)
        used = getattr(acc, "time_used", None)
        total = getattr(acc, "total_time_allowed", None)
        if used is None or not total or not total.total_seconds():
            continue
        left = total - used
        pct = 100 * left.total_seconds() / total.total_seconds()
        lines.append(f"🎮 Kaggle {tag}: {_fmt_hours(used)} из {_fmt_hours(total)} "
                     f"— осталось {_fmt_hours(left)} ({pct:.0f}%)")
    refresh = getattr(q, "quota_refresh_time", None)
    if refresh:
        lines.append(f"🔄 Сброс: {refresh:%d.%m %H:%M} UTC")
    return lines


def render_quota_lines():
    if not RENDER_API_KEY:
        return ["🖥 Render: нет ключа"]
    h = {"Authorization": f"Bearer {RENDER_API_KEY}"}
    base = "https://api.render.com/v1"
    lines = []
    svc = requests.get(f"{base}/services/{RENDER_SERVICE_ID}", headers=h, timeout=20).json()
    svc = svc.get("service", svc)
    det = svc.get("serviceDetails") or {}
    lines.append(f"🖥 Render: {svc.get('name', '?')} · {det.get('plan', '?')} · {det.get('region', '?')}")
    state = "работает" if svc.get("suspended") == "not_suspended" else str(svc.get("suspended"))
    lines.append(f"• статус: {state}")
    deps = requests.get(f"{base}/services/{RENDER_SERVICE_ID}/deploys?limit=1", headers=h, timeout=20).json()
    if deps:
        dep = deps[0].get("deploy", deps[0])
        when = str(dep.get("finishedAt") or dep.get("createdAt") or "")[:16].replace("T", " ")
        lines.append(f"• деплой: {dep.get('status')} · {when} UTC")
    try:
        now = datetime.now(timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        m = requests.get(f"{base}/metrics/bandwidth", headers=h, timeout=20, params={
            "resource": RENDER_SERVICE_ID,
            "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": now.strftime("%Y-%m-%dT%H:%M:%SZ")}).json()
        mb = sum(float(pt.get("value") or 0) for series in m for pt in (series.get("values") or []))
        lines.append(f"• трафик: {_fmt_mb(mb)} / ~100 ГБ")
    except Exception:
        pass
    return lines


async def cmd_quota(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    msg = await update.message.reply_text("📊 Считаю…")

    def _collect():
        lines = []
        try:
            lines += kaggle_quota_lines()
        except Exception as e:
            lines.append(f"🎮 Kaggle: {str(e)[:160]}")
        lines.append("")
        try:
            lines += render_quota_lines()
        except Exception as e:
            lines.append(f"🖥 Render: {str(e)[:160]}")
        return "\n".join(lines).strip()

    try:
        body = await asyncio.to_thread(_collect)
    except Exception as e:
        body = str(e)[:300]
    try:
        await msg.edit_text("📊 Квоты\n\n" + body)
    except Exception:
        await update.message.reply_text("📊 Квоты\n\n" + body)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    print("ОШИБКА:", repr(err), flush=True)
    traceback.print_exception(type(err), err, err.__traceback__ if err else None)
    try:
        if getattr(update, "effective_message", None):
            await update.effective_message.reply_text(f"⚠️ {str(err)[:300]}")
    except Exception:
        pass


def main():
    if not BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN")
    if not BASE_URL:
        raise SystemExit("Нет RENDER_EXTERNAL_URL")
    if not GITHUB_TOKEN:
        print("ВНИМАНИЕ: нет GITHUB_TOKEN — очередь и библиотека не переживут сон Render", flush=True)

    async def _post_init(app):
        await asyncio.to_thread(queue_load)
        await asyncio.to_thread(backgrounds_load)
        app.create_task(queue_worker(app))

    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("quota", cmd_quota))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CallbackQueryHandler(on_button))
    file_filter = (filters.AUDIO | filters.VOICE | filters.VIDEO | filters.VIDEO_NOTE
                   | filters.ANIMATION | filters.Document.ALL)
    app.add_handler(MessageHandler(file_filter, on_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    print(f"Бот запущен. Kaggle: {KAGGLE_USERNAME}/{KERNEL_SLUG}", flush=True)
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=URL_PATH,
        webhook_url=f"{BASE_URL}/{URL_PATH}",
        secret_token=SECRET,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
