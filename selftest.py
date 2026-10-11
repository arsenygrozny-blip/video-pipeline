#!/usr/bin/env python3
"""Проверка рендера без Telegram и без Kaggle.

Гоняет НАСТОЯЩИЙ код из worker.py: нарезку 9:16, склейку фонов, субтитры,
баннер, обложку и финальную сборку. Вместо скачивания с ютуба подставляются
локальные тестовые файлы, вместо распознавания речи — заготовленные слова.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORK = Path(tempfile.mkdtemp(prefix="vp-selftest-"))
FONT_DIR = WORK / "fonts"
DUR = 5.0


def sh(cmd):
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} упал:\n{p.stderr[-2000:]}")
    return p


def load_worker(job):
    """Загружаем worker.py как модуль, но без pip-установки и без запуска main()."""
    src = (ROOT / "worker.py").read_text(encoding="utf-8")
    src = src.replace('_sh("pip install -q -U yt-dlp faster-whisper")', "pass")
    src = re.sub(r"(?m)^_ensure_ffmpeg\(\)$", "pass", src)
    src = re.sub(r"(?m)^main\(\)\s*$", "", src)
    ns = {"__name__": "worker_selftest", "JOB": job}
    exec(compile(src, "worker.py", "exec"), ns)
    ns["WORK"] = WORK
    ns["FONT_DIR"] = FONT_DIR
    for name in ("make_video", "ensure_fonts", "pick_ffmpeg", "make_cover", "make_ass"):
        assert callable(ns[name]), name
    return ns


def make_sources():
    """Собираем тестовые «озвучку», два фона и баннер."""
    audio = WORK / "voice.m4a"
    bgs = []
    for i, color in enumerate(("0x202020", "0x2b2b2b"), 1):
        p = WORK / f"src{i}.mp4"
        sh(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=1280x720:r=30:d=4",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", p])
        bgs.append(p)
    sh(["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={DUR:.0f}",
        "-c:a", "aac", audio])
    banner = WORK / "banner.mp4"
    sh(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0xff0000:s=1080x120:r=30:d=2",
        "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", banner])
    return audio, bgs, banner


FAKE_WORDS = [(0.3, 0.7, "Привет"), (0.75, 1.0, "как"), (1.05, 1.5, "дела"),
              (1.6, 2.0, "тест"), (2.05, 2.3, "субтитров"), (2.6, 2.9, "раз"),
              (2.95, 3.2, "два"), (3.3, 3.8, "и"), (3.85, 4.3, "три")]


def check_pixels(png, boxes):
    """Считаем цветные пиксели в заданных областях кадра."""
    from PIL import Image
    im = Image.open(png).convert("RGB")
    w, h = im.size
    px = im.load()
    res = {}
    for name, (x0, x1, y0, y1) in boxes.items():
        white = red = 0
        for y in range(y0, y1, 2):
            for x in range(x0, x1, 2):
                r, g, b = px[x, y]
                if r > 240 and g > 240 and b > 240:
                    white += 1
                if r > 180 and g < 80 and b < 80:
                    red += 1
        res[name] = (white, red, w, h)
    return res


def main():
    os.environ.setdefault("X264_PRESET", "ultrafast")
    print(f"Рабочая папка теста: {WORK}")
    audio, bgs, banner = make_sources()

    job = {"id": "selftest", "audio": str(audio), "bgs": [str(p) for p in bgs],
           "cover_text": "ТЕСТ ОБЛОЖКИ В ДВЕ СТРОКИ", "banner_file_id": None,
           "token": "0:SELFTEST", "chat_id": 0, "status_msg": 0}
    ns = load_worker(job)

    ns["ensure_fonts"]()
    ns["FF"] = ns["pick_ffmpeg"]()
    print("libass:", ns["HAS_LIBASS"])

    # подменяем сеть и whisper
    def fake_download(url, out_dir, tag, audio=False):
        return Path(url)

    def fake_transcribe(audio_path):
        return list(FAKE_WORDS)

    ns["download"] = fake_download
    ns["transcribe"] = fake_transcribe
    ns["notify"] = lambda t: print("  статус:", t)

    d = WORK / "job"
    d.mkdir(parents=True, exist_ok=True)
    out, cover, dur = ns["make_video"](job, str(banner), d)
    print(f"Готово: {out.name} ({out.stat().st_size / 1024:.0f} КБ), длительность {dur:.2f} c")

    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        info = sh([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                   "stream=width,height,r_frame_rate", "-of", "csv=p=0", out]).stdout.strip()
        print("видео:", info)
        has_audio = sh([ffprobe, "-v", "error", "-select_streams", "a", "-show_entries",
                        "stream=codec_name", "-of", "csv=p=0", out]).stdout.strip()
        print("аудио:", has_audio or "НЕТ")

    frame = WORK / "frame.png"
    sh(["ffmpeg", "-y", "-ss", "1.0", "-i", out, "-frames:v", "1", frame])
    H = 1920
    boxes = {
        "subs": (60, 1020, H - 560 - 130, H - 560 + 130),   # полоса субтитров
        "banner": (200, 880, 170, 270),                      # полоса баннера
        "bg": (60, 300, 900, 1100),                          # фон посередине
    }
    px = check_pixels(frame, boxes)
    sub_white = px["subs"][0]
    banner_red = px["banner"][1]
    bg_dark = px["bg"][0] + px["bg"][1]
    print(f"пиксели: субтитры(белых)={sub_white}, баннер(красных)={banner_red}, фон(ярких)={bg_dark}")

    from PIL import Image
    csz = Image.open(cover).size
    print("обложка:", csz, f"{cover.stat().st_size / 1024:.0f} КБ")

    ok = True
    checks = [
        ("видео на месте", out.exists() and out.stat().st_size > 50_000),
        ("длительность ~5c", abs(dur - DUR) < 0.5),
        ("субтитры вшиты", sub_white > 200),
        ("баннер поверх видео", banner_red > 200),
        ("фон тёмный (кадр 1080x1920)", bg_dark < 40),
        ("обложка 1080x1920", csz == (1080, 1920)),
    ]
    print()
    for name, good in checks:
        print(("  ✅ " if good else "  ❌ ") + name)
        ok = ok and good
    print()
    print("ИТОГ:", "всё работает" if ok else "есть проблемы, смотри выше")
    print(f"Посмотреть результат: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
