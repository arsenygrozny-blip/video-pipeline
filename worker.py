# ВНИМАНИЕ: перед этим файлом бот на Render дописывает строку JOB = json.loads(...)
# с заданием (ссылки, текст обложки, chat_id и т.д.). Здесь JOB уже существует.
import json
import math
import os
import random
import re
import shutil
import subprocess
import threading
import traceback
import urllib.request
from pathlib import Path


def _sh(cmd):
    subprocess.run(cmd, shell=True, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


_sh("pip install -q -U yt-dlp faster-whisper")


def _has_libass(path):
    try:
        out = subprocess.run([path, "-hide_banner", "-filters"],
                             capture_output=True, text=True).stdout
        return " subtitles " in out
    except Exception:
        return False


def _ensure_ffmpeg():
    """На Kaggle в PATH стоит ffmpeg из conda — он без libass, и субтитры не нарисуются.
    Поэтому проверяем не «есть ли ffmpeg», а «умеет ли он subtitles»."""
    for c in ("/usr/bin/ffmpeg", shutil.which("ffmpeg")):
        if c and Path(c).exists() and _has_libass(c):
            print("ffmpeg с libass найден:", c)
            return
    print("Ставлю ffmpeg из apt (нужен libass для субтитров)…")
    _sh("apt-get update -qq && apt-get install -y -qq ffmpeg")
    if not _has_libass("/usr/bin/ffmpeg"):
        print("ВНИМАНИЕ: libass так и не появился, субтитры будут пропущены")


_ensure_ffmpeg()

import requests  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter, ImageFont  # noqa: E402

# ============================ НАСТРОЙКИ ============================
WORK = Path("/kaggle/working")
FONT_DIR = WORK / "fonts"

W, H, FPS = 1080, 1920, 30
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "large-v3")       # small / distil-large-v3 = быстрее
WHISPER_MODEL_CPU = os.environ.get("WHISPER_MODEL_CPU", "small")
WHISPER_LANG = os.environ.get("WHISPER_LANG", "ru") or None
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "auto")          # auto | cuda | cpu
PRESET = os.environ.get("X264_PRESET", "veryfast")                 # ultrafast = быстрее

# Субтитры. Цвета в формате ASS: &H00BBGGRR
SUB = dict(
    font="Montserrat ExtraBold",
    size=84,
    color="&H00FFFFFF",
    outline_color="&H00000000",
    outline=7,
    shadow=0,
    align=2,            # 2 = снизу по центру, 5 = по центру, 8 = сверху
    margin_v=560,
    words=3,
    upper=True,
)

# Баннер. colorkey: None или "0x00FF00:0.3:0.1" (вырезать однотонный фон)
BANNER = dict(pos="top", margin=256, width=1080, colorkey=None)  # 160 + ~5% высоты кадра

# Надпись в начале видео
SUBSCRIBE = dict(
    enabled=True,
    text="Не забудь подписаться",
    start=0.0,       # когда появляется (сек)
    duration=2.5,    # сколько висит (сек)
    y=0.26,          # положение от верха (0.26 = на 26% высоты; было 0.35)
    size=96,         # размер шрифта (было 72)
    upper=False,
)

# Обложка
COVER = dict(
    font="Montserrat-Medium.ttf",
    max_size=170, min_size=50,
    top=0.15,
    width_ratio=0.92,
    max_lines=6,
    first_frame=False,
)

FONT_URLS = {
    "Montserrat-ExtraBold.ttf": "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-ExtraBold.ttf",
    "Montserrat-Medium.ttf": "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-Medium.ttf",
}
# ===================================================================

SUB_DEFAULT = dict(SUB)  # эталон субтитров, от него отсчитываем каждый стиль

# Готовые стили субтитров. Ключ стиля выбирается в боте кнопкой и приходит в JOB["style"].
# Значения накладываются поверх SUB (незаданные берутся из дефолта выше).
SUB_STYLES = {
    "classic": {},
    "shorts":  dict(size=104, words=1, align=5, margin_v=0, outline=9, upper=True),
    "neon":    dict(color="&H0000FFFF", size=94, outline=10, words=2, upper=True),
    "lime":    dict(color="&H0037FF37", size=90, outline=9, words=2, upper=True),
    "minimal": dict(size=64, words=4, outline=5, margin_v=520, upper=False),
}
STYLE_LABELS = {
    "classic": "Классика",
    "shorts": "Shorts",
    "neon": "Неон",
    "lime": "Лайм",
    "minimal": "Минимал",
}


def apply_style(job):
    """Накладываем выбранный стиль на настройки субтитров (SUB)."""
    key = (job.get("style") or "classic").strip().lower()
    preset = SUB_STYLES.get(key)
    if preset is None:
        print("неизвестный стиль, беру classic:", key)
        key, preset = "classic", SUB_STYLES["classic"]
    SUB.clear()
    SUB.update(SUB_DEFAULT)
    SUB.update(preset)
    print(f"стиль субтитров: {key} ({STYLE_LABELS.get(key, key)}) -> {preset}")

TIME_LIMIT_MIN = int(os.environ.get("JOB_TIMEOUT_MIN", "45"))  # предохранитель, минут

FF = "ffmpeg"
HAS_LIBASS = True
API = f"https://api.telegram.org/bot{JOB['token']}"  # noqa: F821
CHAT = JOB["chat_id"]  # noqa: F821
STATUS_MSG = JOB.get("status_msg")  # noqa: F821


# ---------------------------- Telegram ----------------------------
class Cancelled(Exception):
    pass


CANCEL_KB = json.dumps({"inline_keyboard": [[{"text": "🛑 Отменить", "callback_data": "cancel_run"}]]})
NO_KB = json.dumps({"inline_keyboard": []})


_STATUS_OK = False


def notify(text, final=False):
    """Обновляет статус в чате. «Отмена» — это удаление статус-сообщения уже во время работы."""
    global STATUS_MSG, _STATUS_OK
    if not STATUS_MSG:
        return
    try:
        r = requests.post(f"{API}/editMessageText",
                          data={"chat_id": CHAT, "message_id": STATUS_MSG, "text": text,
                                "reply_markup": NO_KB if final else CANCEL_KB},
                          timeout=30).json()
    except Exception:
        return
    if r.get("ok"):
        _STATUS_OK = True
        return
    if "not found" not in str(r.get("description", "")).lower():
        return
    if _STATUS_OK:
        raise Cancelled()
    print("статус-сообщение удалено до старта — продолжаю без него", flush=True)
    try:
        m = requests.post(f"{API}/sendMessage",
                          data={"chat_id": CHAT, "text": "ℹ️ Статус-сообщение удалено, продолжаю работу…"},
                          timeout=30).json()
        if m.get("ok"):
            STATUS_MSG = m["result"]["message_id"]
            _STATUS_OK = True
        else:
            STATUS_MSG = None
    except Exception:
        STATUS_MSG = None


def send_file(method, field, path, extra=None, **data):
    """extra — дополнительные файлы к тому же запросу (например, превью для видео)."""
    files = {field: open(path, "rb")}
    try:
        for name, p in (extra or {}).items():
            files[name] = open(p, "rb")
        r = requests.post(f"{API}/{method}", data={"chat_id": CHAT, **data},
                          files=files, timeout=1200)
        if not r.ok:
            raise RuntimeError(f"Telegram не принял файл: {r.text[:300]}")
    finally:
        for fh in files.values():
            try:
                fh.close()
            except Exception:
                pass


def fetch_telegram_file(file_id, dst):
    r = requests.get(f"{API}/getFile", params={"file_id": file_id}, timeout=60).json()
    if not r.get("ok"):
        raise RuntimeError(f"Не удалось получить баннер из Telegram: {r}")
    url = f"https://api.telegram.org/file/bot{JOB['token']}/{r['result']['file_path']}"  # noqa: F821
    urllib.request.urlretrieve(url, dst)
    return dst


def write_cookies():
    """Кладём cookies.txt (текст из переменной Render COOKIES_TXT) в /kaggle/working.

    Без них YouTube для серверных IP отвечает «Sign in to confirm you're not a bot».
    download() ищет файл именно по этому пути.
    """
    text = (JOB.get("cookies") or "").strip()  # noqa: F821
    if not text:
        print("cookies.txt не задан (COOKIES_TXT пуст) — YouTube может потребовать вход")
        return
    p = WORK / "cookies.txt"
    p.write_text(text + ("" if text.endswith("\n") else "\n"), encoding="utf-8")
    print(f"cookies.txt записан: {len(text)} байт")


def ensure_js_runtime():
    """yt-dlp для YouTube требует JS-рантайм, и по умолчанию признаёт только deno.
    node он без явного --js-runtimes не берёт, поэтому ставим deno (best-effort)."""
    if shutil.which("deno") or Path("/usr/local/bin/deno").exists():
        return
    print("Ставлю deno для yt-dlp…")
    _sh("curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y")
    if Path("/usr/local/bin/deno").exists():
        print("deno установлен")
    else:
        print("ВНИМАНИЕ: deno не установился, часть форматов YouTube будет недоступна")


# ---------------------------- утилиты ----------------------------
def run(cmd, cwd=None):
    p = subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{Path(str(cmd[0])).name} завершился с ошибкой:\n{p.stderr[-1500:]}")
    return p.stdout


def pick_ffmpeg():
    global HAS_LIBASS
    for c in ("/usr/bin/ffmpeg", shutil.which("ffmpeg")):
        if c and Path(c).exists():
            try:
                out = subprocess.run([c, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
                if " subtitles " in out:
                    return c
            except Exception:
                pass
    HAS_LIBASS = False
    return shutil.which("ffmpeg") or "ffmpeg"


def ensure_fonts():
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    for name, url in FONT_URLS.items():
        p = FONT_DIR / name
        if not p.exists():
            try:
                urllib.request.urlretrieve(url, p)
            except Exception as e:
                print("Не удалось скачать шрифт", name, e)


def find_font(name):
    for p in (FONT_DIR / name,
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"):
        if Path(p).exists():
            return str(p)
    return None


def video_size(path):
    """Ширина и высота видео — нужны, чтобы понять, куда встанет баннер."""
    probe = shutil.which("ffprobe")
    if probe:
        try:
            out = run([probe, "-v", "error", "-select_streams", "v:0",
                       "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)])
            w, h = out.strip().splitlines()[0].split(",")[:2]
            return int(w), int(h)
        except Exception:
            pass
    err = subprocess.run([FF, "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    m = re.search(r", (\d{2,5})x(\d{2,5})", err)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def duration(path):
    probe = shutil.which("ffprobe")
    if probe:
        try:
            out = run([probe, "-v", "error", "-show_entries", "format=duration",
                       "-of", "default=nw=1:nk=1", path])
            return float(out.strip())
        except Exception:
            pass
    err = subprocess.run([FF, "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d{2}):(\d{2}(?:\.\d+)?)", err)
    if not m:
        raise RuntimeError("Не смог определить длительность озвучки")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


# ---------------------------- скачивание ----------------------------
JS_CANDIDATES = {
    "deno": ["/usr/local/bin/deno", "/root/.deno/bin/deno", "/usr/bin/deno"],
    "node": ["/tools/node/bin/node", "/usr/local/bin/node", "/usr/bin/node", "/usr/bin/nodejs"],
    "bun": ["/usr/local/bin/bun", "/root/.bun/bin/bun"],
}


def find_js_runtime():
    """Ищем JS-рантайм для yt-dlp. PATH в ядре Kaggle бывает урезан, поэтому
    проверяем и известные пути вручную."""
    for name, paths in JS_CANDIDATES.items():
        found = shutil.which(name)
        if found:
            return name, None
        for path in paths:
            if Path(path).exists():
                return name, path
    return None, None


def download(url, out_dir, tag, audio=False):
    """Скачиваем через yt-dlp, не полагаясь на удачу.

    Свежий yt-dlp хочет JS-рантайм — на Kaggle есть node, и с ним находится
    больше форматов. Но если с рантаймом что-то не так, повторяем без него.
    Плюс перебираем несколько форматов: не отдался лучший — берём попроще.
    """
    import yt_dlp

    base = {
        "outtmpl": str(out_dir / f"{tag}_%(id)s.%(ext)s"),
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    }
    cookies = WORK / "cookies.txt"
    if cookies.exists():
        base["cookiefile"] = str(cookies)

    js, js_path = find_js_runtime()
    if js:
        print(f"yt-dlp: JS-рантайм {js}" + (f" ({js_path})" if js_path else " (из PATH)"))
    fmts = ["bestaudio/best", "best"] if audio else ["bv*/b", "b", "best"]
    errors = []
    for runtime in ([js] if js else []) + [None]:
        for fmt in fmts:
            opts = dict(base, format=fmt)
            if runtime:
                opts["js_runtimes"] = {runtime: ({"path": js_path} if js_path else {})}
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    try:
                        return Path(info["requested_downloads"][0]["filepath"])
                    except Exception:
                        return Path(ydl.prepare_filename(info))
            except Exception as e:
                errors.append(f"{fmt}{' + ' + runtime if runtime else ''}: {str(e)[:110]}")
    raise RuntimeError(
        "Не получилось скачать:\n" + "\n".join(errors[-4:]) + "\n\n"
        "Если YouTube пишет «Sign in to confirm you're not a bot» — нужны cookies: "
        "выгрузи cookies.txt (Netscape) и впиши его текст в переменную COOKIES_TXT на Render."
    )


def resolve(src, out_dir, tag, audio=False):
    """Источник может быть ссылкой (str) или файлом, присланным в Telegram
    (dict вида {"file_id": ...}) — тогда тянем его через Bot API.
    Также поддерживает dict из библиотеки фонов: {"url": ..., "name": ..., ...}."""
    if isinstance(src, dict):
        if src.get("file_id"):
            dst = Path(out_dir) / f"{tag}.upload"
            print(f"{tag}: файл из Telegram {src['file_id']}")
            return fetch_telegram_file(src["file_id"], dst)
        if src.get("url"):
            return download(src["url"], out_dir, tag, audio=audio)
    return download(src, out_dir, tag, audio=audio)


def normalize(src, dst, dur, start_time=None, end_time=None, loop=True):
    """Нормализует фон под 9:16.
    
    start_time — секунда старта в исходном фоне (None = случайная, если фон длиннее dur)
    end_time   — конец обрезки (None = до конца файла)
    loop       — зацикливать, если фон короче dur
    """
    bg_dur = duration(src)

    # Определяем стартовую точку
    if start_time is None:
        if bg_dur > dur:
            start_time = random.uniform(0, max(0, bg_dur - dur))
        else:
            start_time = 0.0

    # Определяем конец сегмента
    seg_end = min(start_time + dur, bg_dur)
    if end_time is not None:
        seg_end = min(seg_end, end_time)
    seg_dur = max(seg_end - start_time, 0.1)

    # Извлекаем сегмент
    segment = dst.parent / f"{dst.stem}_seg.mp4"
    run([FF, "-y", "-ss", f"{start_time:.3f}", "-i", src, "-t", f"{seg_dur:.3f}",
         "-an", "-c", "copy", segment])

    if seg_dur >= dur:
        # Сегмента хватает — просто копируем с обрезкой по dur
        run([FF, "-y", "-i", segment, "-an", "-t", f"{dur + 1:.2f}", "-c", "copy", dst])
    elif loop:
        # Фон короче видео — зацикливаем сегмент
        loops = math.ceil(dur / seg_dur)
        lst = dst.parent / f"{dst.stem}_loop.txt"
        lst.write_text("".join(f"file '{segment.resolve()}'\n" for _ in range(loops)))
        run([FF, "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
             "-t", f"{dur + 1:.2f}", dst])
        lst.unlink(missing_ok=True)
    else:
        run([FF, "-y", "-i", segment, "-an", "-t", f"{dur + 1:.2f}", "-c", "copy", dst])

    segment.unlink(missing_ok=True)


def concat(clips, dst):
    lst = dst.parent / "list.txt"
    lst.write_text("".join(f"file '{Path(c).resolve()}'\n" for c in clips))
    run([FF, "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", dst])


# ---------------------------- субтитры ----------------------------
_models = {}


def get_model(device):
    from faster_whisper import WhisperModel
    if device not in _models:
        if device == "cuda":
            _models[device] = WhisperModel(WHISPER_MODEL, device="cuda", compute_type="float16")
        else:
            _models[device] = WhisperModel(WHISPER_MODEL_CPU, device="cpu", compute_type="int8")
    return _models[device]


def install_cudnn():
    """faster-whisper на GPU требует cuDNN 9 — если его нет, доставляем колёсами nvidia."""
    _sh("pip install -q -U nvidia-cudnn-cu12 nvidia-cublas-cu12")
    import glob
    libs = []
    for pat in ("/usr/local/lib/python3*/dist-packages/nvidia/*/lib",
                "/opt/conda/lib/python3*/site-packages/nvidia/*/lib",
                "/usr/lib/python3/dist-packages/nvidia/*/lib"):
        libs += glob.glob(pat)
    if libs:
        os.environ["LD_LIBRARY_PATH"] = ":".join(libs + [os.environ.get("LD_LIBRARY_PATH", "")])


def load_audio(path):
    """Декодируем звук в 16 кГц моно сами, через ffmpeg.

    faster-whisper внутри зовёт PyAV: av.open(..., metadata_errors="ignore"), а сборка
    av в образе Kaggle такого аргумента не знает и валится с TypeError
    "open() got an unexpected keyword argument 'metadata_errors'". Поэтому отдаём в
    модель готовый массив сэмплов — этот путь от av не зависит вообще.
    """
    import numpy as np
    pcm = Path(path).with_suffix(".pcm")
    run([FF, "-y", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", "16000", str(pcm)])
    try:
        return np.fromfile(pcm, dtype=np.float32)
    finally:
        pcm.unlink(missing_ok=True)


def _recognize(model, audio):
    segs, _ = model.transcribe(audio, language=WHISPER_LANG, word_timestamps=True, vad_filter=True)
    words = []
    for s in segs:
        for w in (s.words or []):
            t = w.word.strip()
            if t:
                words.append((w.start, w.end, t))
    return words


def transcribe(audio_path):
    audio = load_audio(audio_path)
    devices = ["cuda", "cpu"] if WHISPER_DEVICE == "auto" else [WHISPER_DEVICE]
    last = None
    for dev in devices:
        try:
            words = _recognize(get_model(dev), audio)
            print(f"whisper ok на {dev}, слов: {len(words)}")
            return words
        except Exception as e:
            last = e
            print("whisper", dev, "не сработал:", e)
            if dev == "cuda":
                install_cudnn()
                _models.pop("cuda", None)
                try:
                    words = _recognize(get_model("cuda"), audio)
                    print(f"whisper ok на cuda после установки cuDNN, слов: {len(words)}")
                    return words
                except Exception as e2:
                    last = e2
                    print("whisper cuda после cuDNN тоже не сработал:", e2)
    raise RuntimeError(f"Не удалось распознать речь: {last}")


def ass_time(t):
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def chunk_words(words):
    lines, cur = [], []
    for st, en, w in words:
        if cur and (len(cur) >= SUB["words"] or st - cur[-1][1] > 0.7):
            lines.append(cur)
            cur = []
        cur.append((st, en, w))
        if w[-1] in ".!?…":
            lines.append(cur)
            cur = []
    if cur:
        lines.append(cur)
    return lines


def make_ass(words, path, sub_y_px=None):
    if not words and not SUBSCRIBE["enabled"]:
        return False
    s = SUB
    sb = SUBSCRIBE
    sub_top = int(sub_y_px) if sub_y_px else int(H * sb["y"])
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {W}\nPlayResY: {H}\nWrapStyle: 2\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{s['font']},{s['size']},{s['color']},&H000000FF,{s['outline_color']},&H00000000,"
        f"0,0,0,0,100,100,0,0,1,{s['outline']},{s['shadow']},{s['align']},60,60,{s['margin_v']},1\n"
        f"Style: Sub,{s['font']},{sb['size']},{s['color']},&H000000FF,{s['outline_color']},&H00000000,"
        f"0,0,0,0,100,100,0,0,1,{s['outline']},{s['shadow']},8,60,60,{sub_top},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    groups = chunk_words(words) if words else []
    rows = []
    if sb["enabled"]:
        t = sb["text"].upper() if sb["upper"] else sb["text"]
        rows.append(f"Dialogue: 1,{ass_time(sb['start'])},{ass_time(sb['start'] + sb['duration'])},"
                    "Sub,,0,0,0,," + "{\\fad(300,300)}" + t)
    for i, g in enumerate(groups):
        start = g[0][0]
        end = max(g[-1][1], start + 0.2)
        if i + 1 < len(groups) and groups[i + 1][0][0] - end < 0.25:
            end = groups[i + 1][0][0]
        text = " ".join(w.strip(".,;:") for _, _, w in g)
        if s["upper"]:
            text = text.upper()
        rows.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{text}")
    Path(path).write_text(head + "\n".join(rows) + "\n", encoding="utf-8")
    return True


# ---------------------------- обложка ----------------------------
def wrap_text(d, text, font, maxw):
    lines = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split():
            t = (cur + " " + word).strip()
            if not cur or d.textlength(t, font=font) <= maxw:
                cur = t
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
    return lines


def make_cover(frame_path, text, out_path):
    img = Image.open(frame_path).convert("RGB").resize((W, H))
    d = ImageDraw.Draw(img)
    fp = find_font(COVER["font"])
    maxw = int(W * COVER["width_ratio"])

    def load(size):
        return ImageFont.truetype(fp, size) if fp else ImageFont.load_default()

    chosen = None
    for size in range(COVER["max_size"], COVER["min_size"] - 1, -4):
        font = load(size)
        lines = wrap_text(d, text, font, maxw)
        lh = int(size * 1.12)
        if (len(lines) <= COVER["max_lines"] and lh * len(lines) <= H * 0.5
                and all(d.textlength(l, font=font) <= maxw for l in lines)):
            chosen = (font, lines, lh)
            break
    if not chosen:
        font = load(COVER["min_size"])
        chosen = (font, wrap_text(d, text, font, maxw), int(COVER["min_size"] * 1.12))
    font, lines, lh = chosen

    y = int(H * COVER["top"])
    pos = []
    for l in lines:
        x = (W - d.textlength(l, font=font)) / 2
        pos.append((x, y, l))
        y += lh

    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    for x, y, l in pos:
        sd.text((x + 4, y + 5), l, font=font, fill=(0, 0, 0, 150))
    shadow = shadow.filter(ImageFilter.GaussianBlur(5))
    img = Image.alpha_composite(img.convert("RGBA"), shadow)
    d2 = ImageDraw.Draw(img)
    for x, y, l in pos:
        d2.text((x, y), l, font=font, fill=(255, 255, 255, 255))
    img.convert("RGB").save(out_path, quality=95)


# ---------------------------- сборка ----------------------------
def assemble(bg, audio, ass, banner, cover, dur, out):
    inputs = ["-stream_loop", "-1", "-i", bg, "-i", audio]
    chain, cur, idx = [], "[0:v]", 2
    chain.append(f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                 f"crop={W}:{H},setsar=1[bg0]")
    cur = "[bg0]"

    if banner:
        inputs += ["-stream_loop", "-1", "-i", banner]
        b = f"[{idx}:v]"
        if BANNER["colorkey"]:
            b += f"format=rgba,colorkey={BANNER['colorkey']},"
        b += (f"scale={BANNER['width']}:{int(H * 0.3)}:force_original_aspect_ratio=decrease:"
              f"force_divisible_by=2,setsar=1[bn]")
        y = f"H-h-{BANNER['margin']}" if BANNER["pos"] == "bottom" else str(BANNER["margin"])
        chain += [b, f"{cur}[bn]overlay=(W-w)/2:{y}[v1]"]
        cur = "[v1]"
        idx += 1

    if ass and HAS_LIBASS:
        chain.append(f"{cur}subtitles={ass.name}:fontsdir={FONT_DIR}[v2]")
        cur = "[v2]"

    if cover and COVER["first_frame"]:
        inputs += ["-loop", "1", "-t", "1", "-i", cover]
        chain += [f"[{idx}:v]scale={W}:{H}[cv]",
                  f"{cur}[cv]overlay=0:0:enable='lt(t,0.12)'[v3]"]
        cur = "[v3]"
        idx += 1

    chain.append(f"{cur}format=yuv420p[vout]")
    vk = min(9000, max(800, int(360000 / dur) - 250))  # чтобы влезть в ~45 МБ (лимит Telegram 50)
    cmd = [FF, "-y", *inputs, "-filter_complex", ";".join(chain),
           "-map", "[vout]", "-map", "1:a", "-t", f"{dur:.2f}", "-r", str(FPS),
           "-c:v", "libx264", "-preset", PRESET, "-crf", "21",
           "-maxrate", f"{vk}k", "-bufsize", f"{vk * 2}k",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out]
    run(cmd, cwd=ass.parent if ass else None)


def make_video(job, banner, d):
    print("make_video: старт", flush=True)
    apply_style(job)
    notify("⬇️ Скачиваю озвучку…")
    raw = resolve(job["audio"], d, "voice", audio=True)
    audio = d / "audio.m4a"
    run([FF, "-y", "-i", raw, "-vn", "-c:a", "aac", "-b:a", "192k", audio])
    dur = duration(audio)

    clips = []
    total = len(job["bgs"])
    for i, ref in enumerate(job["bgs"], 1):
        notify(f"⬇️ Фон {i}/{total}: скачиваю и подгоняю под 9:16…")
        # Извлекаем метаданные фона из библиотеки (если dict)
        bg_start = None
        bg_end = None
        if isinstance(ref, dict):
            bg_end = ref.get("end_time")
        src = resolve(ref, d, f"bg{i}")
        dst = d / f"norm_{i}.mp4"
        normalize(src, dst, dur, start_time=bg_start, end_time=bg_end)
        clips.append(dst)

    bg = d / "bg.mp4"
    if len(clips) == 1:
        shutil.copy(clips[0], bg)
    else:
        notify("🔗 Склеиваю фоны…")
        concat(clips, bg)

    notify("📝 Распознаю речь…")
    words = transcribe(audio)
    ass = d / "subs.ass"
    sub_top = None
    if banner:
        bw, bh = video_size(banner)
        if bw:
            # надпись «подпишись» ставим под баннером, чтобы не наезжала на него
            sub_top = BANNER["margin"] + round(bh * BANNER["width"] / bw) + 40
            print(f"баннер {bw}x{bh} → надпись начнётся с {sub_top}px")
    ass_ok = make_ass(words, ass, sub_top)

    cover = None
    if job.get("cover_text"):
        notify("🖼 Делаю обложку…")
        base = d / "base.jpg"
        run([FF, "-y", "-ss", "0.5", "-i", bg, "-frames:v", "1", base])
        cover = d / "cover.jpg"
        make_cover(base, job["cover_text"], cover)

    notify("🎞 Собираю видео…")
    out = d / "result.mp4"
    assemble(bg, audio, ass if ass_ok else None, banner, cover, dur, out)
    return out, cover, dur


def _watchdog():
    try:
        requests.post(f"{API}/sendMessage",
                      data={"chat_id": CHAT,
                            "text": f"⏰ Задание шло дольше {TIME_LIMIT_MIN} минут и было остановлено."},
                      timeout=30)
        if STATUS_MSG:
            requests.post(f"{API}/editMessageReplyMarkup",
                          data={"chat_id": CHAT, "message_id": STATUS_MSG, "reply_markup": NO_KB}, timeout=30)
    except Exception:
        pass
    os._exit(1)


def main():
    global FF
    wd = threading.Timer(TIME_LIMIT_MIN * 60, _watchdog)
    wd.daemon = True
    wd.start()
    d = WORK / "job"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    try:
        print("main: старт", flush=True)
        notify("⚙️ Kaggle запустился, готовлюсь…")
        ensure_fonts()
        write_cookies()
        ensure_js_runtime()
        FF = pick_ffmpeg()
        print("ffmpeg:", FF, flush=True)
        banner = None
        if JOB.get("banner_file_id"):  # noqa: F821
            banner = fetch_telegram_file(JOB["banner_file_id"], d / "banner.bin")  # noqa: F821
            print("баннер скачан", flush=True)
        out, cover, dur = make_video(JOB, banner, d)  # noqa: F821
        notify("📤 Отправляю…")
        thumb = None
        if cover:
            # превью для плеера: маленькая копия обложки
            try:
                img = Image.open(cover)
                img.thumbnail((320, 320))
                thumb = d / "thumb.jpg"
                img.convert("RGB").save(thumb, quality=85)
            except Exception as e:
                print("превью не сделалось:", e)
                thumb = None
        send_file("sendVideo", "video", out,
                  extra={"thumbnail": thumb} if thumb else None,
                  supports_streaming="true", width=W, height=H, duration=int(dur))
        if cover:
            send_file("sendDocument", "document", cover, caption="Обложка (файлом, без сжатия)")
        notify("✅ Готово", final=True)
    except Cancelled:
        try:
            requests.post(f"{API}/sendMessage", data={"chat_id": CHAT, "text": "🛑 Задание остановлено."}, timeout=30)
        except Exception:
            pass
    except Exception:
        err = traceback.format_exc()
        print(err)
        try:
            requests.post(f"{API}/sendMessage",
                          data={"chat_id": CHAT, "text": "❌ Ошибка на Kaggle:\n" + err[-900:]}, timeout=30)
            if STATUS_MSG:
                requests.post(f"{API}/editMessageReplyMarkup",
                              data={"chat_id": CHAT, "message_id": STATUS_MSG, "reply_markup": NO_KB}, timeout=30)
        except Exception:
            pass
    finally:
        wd.cancel()
        shutil.rmtree(d, ignore_errors=True)


main()
