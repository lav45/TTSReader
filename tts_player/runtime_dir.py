"""Каталог состояния плеера на время сеанса и пути в нём, которые делят разные процессы.

Linux: $XDG_RUNTIME_DIR (/run/user/<uid>). В macOS этой переменной нет: там $TMPDIR — личный
каталог пользователя (/var/folders/…), который тоже чистит перезагрузка. Только стандартная
библиотека: модуль импортируют хук и приёмник звука, которые зовут на каждом событии.
"""
import os
import platform
from collections.abc import Mapping
from pathlib import Path

APP_DIR = "tts-player"
MISSING = "XDG_RUNTIME_DIR не задан"


def runtime_dir(environ: Mapping[str, str] = os.environ, system: str | None = None) -> Path | None:
    """$XDG_RUNTIME_DIR; без него на macOS — $TMPDIR; иначе None (сообщение — MISSING)."""
    runtime = environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime)
    if (system or platform.system()) == "Darwin" and environ.get("TMPDIR"):
        return Path(environ["TMPDIR"])
    return None


def inbox_dir(runtime: Path) -> Path:
    """Буфер звука (контракт (13)): пишет `tts-player sink`, забирает плеер."""
    return runtime / APP_DIR / "inbox"


def hook_queue_dir(runtime: Path) -> Path:
    """Очередь задач хука (контракт (11)): `claude-tts speak` хука пишет сюда
    (CLAUDE_TTS_QUEUE_DIR), плеер забирает."""
    return runtime / APP_DIR / "hook-queue"
