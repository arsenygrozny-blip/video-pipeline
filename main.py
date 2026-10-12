import asyncio
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
COOKIES_TXT = os.environ.get("COOKIES_TXT", "")  # необязательно: cookies.txt для yt-dlp (текстом)
KERNEL_SLUG = os.environ.get("KERNEL_SLUG", "video-worker")
RENDER_API_KEY = os.environ.get("RENDER_API_KEY", "")  # чтобы показывать квоты Render
RENDER_SERVICE_ID = os.environ.get("RENDER_SERVICE_ID", "srv-db5u35942hec73a7dek0")
# =======================================================================================================

SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
URL_PATH = hashlib.sha256((BOT_TOKEN + "path").encode()).hexdigest()[:24]
WORKER_FILE = Path(__file__).parent / "worker.py"
if not WORKER_FILE.exists():
    raise SystemExit("Рядом с main.py должен лежать worker.py — это код, который запустится на Kaggle")
WORKER_SRC = WORKER_FILE.read_text(encoding="utf-8")
URL_RE = re.compile(r"https?://\S+")

# Последний баннер. После «сна» сервера память стирается, поэтому его можно
# зафиксировать переменной BANNER_FILE_ID.
STATE = {"banner": os.environ.get("BANNER_FILE_ID", "")}

MENU = ReplyKeyboardMarkup([["🎬 Новое видео", "📊 Квоты"]], resize_keyboard=True)
CANCEL = ("❌ Отмена", "cancel")


class Busy(Exception):
    pass


def kb(*rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=c) for t, c in row] for row in rows])


# ---------------------------- запуск Kaggle ----------------------------
def kaggle_api():
    """Kaggle API + понятная ошибка, если не заданы ключи."""
    from kaggle.api.kaggle_api_extended import KaggleApi

    if not os.environ.get("KAGGLE_USERNAME") or not os.environ.get("KAGGLE_KEY"):
        raise RuntimeError("На Render не заданы KAGGLE_USERNAME и/или KAGGLE_KEY. "
                           "Возьми их в Kaggle → Settings → API → Create New Token.")
    api = KaggleApi()
    try:
        api.authenticate()
    except Exception as e:
        raise RuntimeError(f"Kaggle не принял ключи: {e}") from e
    return api


def kaggle_run(job):
    api = kaggle_api()
    slug = f"{KAGGLE_USERNAME}/{KERNEL_SLUG}"

    # Если прошлое задание ещё идёт, новое не запускаем (иначе оно перезапишет текущее)
    try:
        st = api.kernels_status(slug)
        status = str(getattr(st, "status", st)).lower()
        if "running" in status or "queued" in status:
            raise Busy()
    except Busy:
        raise
    except Exception:
        pass  # ядра ещё нет, это первый запуск

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


def kaggle_cancel():
    """Пробуем жёстко остановить сессию через Kaggle API. Возвращает True, если вызов прошёл."""
    try:
        api = kaggle_api()
    except Exception as e:
        print("Kaggle cancel: нет доступа к API:", e)
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
    print("В этой версии kaggle нет метода отмены, останется мягкая отмена")
    return False


# ---------------------------- диалог ----------------------------
async def guard(update: Update):
    uid = update.effective_user.id
    if ALLOWED_USER_ID == 0:
        await update.effective_message.reply_text(
            f"Твой ID: {uid}\nВпиши его в переменную ALLOWED_USER_ID на Render и сохрани.")
        return False
    return uid == ALLOWED_USER_ID


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text("Готов к работе. Жми «🎬 Новое видео».", reply_markup=MENU)


def _track(context, msg):
    """Запоминаем id сообщений текущего диалога, чтобы потом подчистить чат."""
    mid = getattr(msg, "message_id", None)
    if mid:
        context.user_data.setdefault("flow_msgs", []).append(mid)


async def cleanup_flow(chat_id, context, bot):
    """Удаляем сообщения предыдущего диалога. Свои удаляются всегда, сообщения
    пользователя — как получится (Telegram не гарантирует это для старых)."""
    for mid in context.user_data.pop("flow_msgs", []):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception as e:
            print("не удалил сообщение", mid, ":", e)


async def start_job(msg, context):
    await cleanup_flow(msg.chat_id, context, context.bot)
    context.user_data["job"] = {"id": uuid.uuid4().hex[:8], "step": "audio", "bgs": []}
    _track(context, msg)
    _track(context, await msg.reply_text("1/4. Пришли ссылку на озвучку.", reply_markup=kb([CANCEL])))


async def ask_bgs(msg, context):
    _track(context, await msg.reply_text(
        "2/4. Пришли ссылки на фоны: по одной или несколькими сообщениями. "
        "Они склеятся в том порядке, в котором пришли. Когда хватит, жми «Готово».",
        reply_markup=kb([("✅ Готово", "bgs_done")], [CANCEL])))


async def ask_banner(msg, context):
    row = [("⏭ Пропустить", "skip_banner")]
    if STATE["banner"]:
        row.insert(0, ("♻️ Прошлый баннер", "use_banner"))
    _track(context, await msg.reply_text(
        "3/4. Пришли баннер файлом (mp4/gif/webm, до 20 МБ) или пропусти.",
        reply_markup=kb(row, [CANCEL])))


async def ask_cover(msg, context):
    _track(context, await msg.reply_text(
        "4/4. Напиши текст обложки или пропусти.",
        reply_markup=kb([("⏭ Пропустить", "skip_cover")], [CANCEL])))


async def show_ready(msg, job, context):
    cover = job.get("cover_text")
    txt = (f"Всё готово к сборке:\n• озвучка: есть\n• фонов: {len(job['bgs'])}\n"
           f"• баннер: {'да' if job.get('banner_file_id') else 'нет'}\n"
           f"• обложка: {('«' + cover[:60] + '»') if cover else 'нет'}")
    _track(context, await msg.reply_text(txt, reply_markup=kb([("🚀 Создать", "create")], [CANCEL])))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    msg = update.message
    text = (msg.text or "").strip()
    if text == "🎬 Новое видео":
        await start_job(msg, context)
        return
    if text == "📊 Квоты":
        await cmd_quota(update, context)
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
            await msg.reply_text("Нужна ссылка (http…).")
            return
        job["audio"] = urls[0]
        job["step"] = "bgs"
        await ask_bgs(msg, context)
    elif step == "bgs":
        urls = URL_RE.findall(text)
        if not urls:
            await msg.reply_text("Нужна ссылка (http…).")
            return
        job["bgs"] += urls
        _track(context, await msg.reply_text(
            f"Фонов: {len(job['bgs'])}. Ещё ссылку или «Готово».",
            reply_markup=kb([("✅ Готово", "bgs_done")], [CANCEL])))
    elif step == "cover":
        job["cover_text"] = text
        job["step"] = "ready"
        await show_ready(msg, job, context)
    elif step == "banner":
        await msg.reply_text("Тут нужен файл баннера. Отправь его как файл или нажми «Пропустить».")
    else:
        await msg.reply_text("Жми «🚀 Создать» или «Отмена».")


async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    msg = update.message
    job = context.user_data.get("job")
    if not job or job["step"] != "banner":
        await msg.reply_text("Файл сейчас не нужен. Жми «🎬 Новое видео».", reply_markup=MENU)
        return
    obj = msg.video or msg.animation or msg.document
    if obj.file_size and obj.file_size > 20 * 1024 * 1024:
        await msg.reply_text("Файл больше 20 МБ, Telegram не даёт ботам его скачать. Сожми баннер и пришли заново.")
        return
    STATE["banner"] = obj.file_id
    job["banner_file_id"] = obj.file_id
    job["step"] = "cover"
    _track(context, msg)
    _track(context, await msg.reply_text(
        "Баннер принят. Чтобы он запоминался даже после «сна» сервера, впиши в Render "
        f"переменную BANNER_FILE_ID:\n{obj.file_id}"))
    await ask_cover(msg, context)


async def safe_edit(m, t, markup=None):
    try:
        await m.edit_text(t, reply_markup=markup)
    except Exception:
        pass


async def launch(chat_id, job, bot):
    status = await bot.send_message(chat_id, "🚀 Отправляю задание на Kaggle…")
    job = dict(job)
    job.update({
        "token": BOT_TOKEN,
        "chat_id": chat_id,
        "status_msg": status.message_id,
        "cookies": COOKIES_TXT,
    })
    for k in ("step",):
        job.pop(k, None)
    try:
        await asyncio.to_thread(kaggle_run, job)
        await safe_edit(status, "⏳ Задание ушло на Kaggle. Старт занимает несколько минут, "
                                "дальше здесь появятся статусы и готовое видео.",
                        kb([("🛑 Отменить", "cancel_run")]))
    except Busy:
        await safe_edit(status, "⏳ Предыдущее видео ещё собирается на Kaggle. "
                                "Дождись его и запусти это заново.")
    except Exception as e:
        print("LAUNCH ERROR:", e)
        await safe_edit(status, f"❌ Не удалось запустить Kaggle:\n{str(e)[-700:]}")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await guard(update):
        return
    try:
        await q.answer()
    except Exception as e:
        # кнопка могла устареть (Telegram отвечает "query is too old") — это не повод падать
        print("callback answer:", e)
    data = q.data
    msg = q.message
    job = context.user_data.get("job")

    if data == "cancel_run":
        # Мягкая отмена: удаляем статус-сообщение, воркер это замечает на ближайшем шаге и выходит.
        # Параллельно пробуем жёстко остановить сессию через Kaggle API.
        try:
            await msg.delete()
        except Exception:
            pass
        await context.bot.send_message(msg.chat_id, "🛑 Отмена отправлена. Остановится в течение пары минут.",
                                       reply_markup=MENU)
        await asyncio.to_thread(kaggle_cancel)
        return

    if data == "cancel":
        context.user_data.pop("job", None)
        await cleanup_flow(msg.chat_id, context, context.bot)
        await msg.reply_text("Отменено.", reply_markup=MENU)
        return
    if not job:
        await msg.reply_text("Это уже неактуально (сервер мог заснуть). Жми «🎬 Новое видео».", reply_markup=MENU)
        return

    if data == "bgs_done":
        if not job["bgs"]:
            await msg.reply_text("Нужна хотя бы одна ссылка на фон.")
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
        await cleanup_flow(msg.chat_id, context, context.bot)
        await launch(msg.chat_id, job, context.bot)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать, что сейчас происходит на Kaggle."""
    if not await guard(update):
        return
    def _check():
        api = kaggle_api()
        st = api.kernels_status(f"{KAGGLE_USERNAME}/{KERNEL_SLUG}")
        return str(getattr(st, "status", st))
    try:
        status = await asyncio.to_thread(_check)
        await update.message.reply_text(f"Kaggle ({KAGGLE_USERNAME}/{KERNEL_SLUG}): {status}")
    except Exception as e:
        await update.message.reply_text(f"Не получил статус: {str(e)[-400:]}")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Твой Telegram ID: {update.effective_user.id}")


# ---------------------------- квоты ----------------------------
def _fmt_hours(td):
    secs = int(round(td.total_seconds()))
    h, m = divmod(secs // 60, 60)
    return f"{h} ч {m:02d} мин" if h else f"{m} мин"


def _fmt_mb(mb):
    return f"{mb / 1024:.2f} ГБ" if mb >= 1024 else f"{mb:.1f} МБ"


def kaggle_quota_lines():
    """Остаток ускорительной квоты Kaggle (GPU/TPU) и дата сброса."""
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
        lines.append(f"🔄 Сброс квоты: {refresh:%d.%m.%Y %H:%M} UTC")
    return lines


def render_quota_lines():
    """Тариф, статус, последний деплой и трафик сервиса на Render."""
    if not RENDER_API_KEY:
        return ["🖥 Render: нет ключа (добавь переменную RENDER_API_KEY)"]
    h = {"Authorization": f"Bearer {RENDER_API_KEY}"}
    base = "https://api.render.com/v1"
    lines = []
    svc = requests.get(f"{base}/services/{RENDER_SERVICE_ID}", headers=h, timeout=20).json()
    svc = svc.get("service", svc)
    det = svc.get("serviceDetails") or {}
    lines.append(f"🖥 Render: {svc.get('name', '?')} · план {det.get('plan', '?')} · {det.get('region', '?')}")
    state = "работает" if svc.get("suspended") == "not_suspended" else str(svc.get("suspended"))
    lines.append(f"• статус: {state}")
    deps = requests.get(f"{base}/services/{RENDER_SERVICE_ID}/deploys?limit=1", headers=h, timeout=20).json()
    if deps:
        dep = deps[0].get("deploy", deps[0])
        when = str(dep.get("finishedAt") or dep.get("createdAt") or "")[:16].replace("T", " ")
        lines.append(f"• последний деплой: {dep.get('status')} · {when} UTC")
    try:
        now = datetime.now(timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        m = requests.get(f"{base}/metrics/bandwidth", headers=h, timeout=20, params={
            "resource": RENDER_SERVICE_ID,
            "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": now.strftime("%Y-%m-%dT%H:%M:%SZ")}).json()
        mb = sum(float(pt.get("value") or 0) for series in m for pt in (series.get("values") or []))
        lines.append(f"• трафик за месяц: {_fmt_mb(mb)} (лимит free ~100 ГБ)")
    except Exception as e:
        print("Render bandwidth:", e)
    lines.append("• free: 750 инстанс-часов/мес, сон через ~15 мин простоя")
    return lines


async def cmd_quota(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать остаток квот на Kaggle и Render."""
    if not await guard(update):
        return
    msg = await update.message.reply_text("📊 Считаю квоты…")

    def _collect():
        lines = []
        try:
            lines += kaggle_quota_lines()
        except Exception as e:
            lines.append(f"🎮 Kaggle: не получил ({str(e)[:160]})")
        lines.append("")
        try:
            lines += render_quota_lines()
        except Exception as e:
            lines.append(f"🖥 Render: не получил ({str(e)[:160]})")
        return "\n".join(lines).strip()

    try:
        body = await asyncio.to_thread(_collect)
    except Exception as e:
        body = f"Не получилось: {str(e)[:300]}"
    header = "📊 Квоты\n\n"
    try:
        await msg.edit_text(header + body)
    except Exception:
        await update.message.reply_text(header + body)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Чтобы падение в обработчике не пропадало молча."""
    err = context.error
    print("ОШИБКА БОТА:", repr(err))
    traceback.print_exception(type(err), err, err.__traceback__ if err else None)
    try:
        if getattr(update, "effective_message", None):
            await update.effective_message.reply_text(f"⚠️ Внутренняя ошибка: {str(err)[:300]}")
    except Exception:
        pass


def main():
    if not BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN")
    if not BASE_URL:
        raise SystemExit("Нет RENDER_EXTERNAL_URL (или WEBHOOK_BASE_URL)")
    missing = [k for k in ("KAGGLE_USERNAME", "KAGGLE_KEY") if not os.environ.get(k)]
    if missing:
        print("ВНИМАНИЕ: не заданы " + ", ".join(missing) + " — задания не будут уходить на Kaggle")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("quota", cmd_quota))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.VIDEO | filters.ANIMATION | filters.Document.ALL, on_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    print(f"Бот запущен (webhook). Ядро на Kaggle: {KAGGLE_USERNAME}/{KERNEL_SLUG}")
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
