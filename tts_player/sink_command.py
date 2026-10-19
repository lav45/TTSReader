"""Подкоманда `tts-player sink [опции] <wav>`: проигрыватель демона claude-tts (player_command в
~/.claude-tts/config.json). Не играет, а кладёт фразу в буфер плеера и сразу выходит; звук
играет плеер. Модуль не импортирует плеер и gi: демон зовёт его на каждое предложение.

Файл: <runtime>/tts-player/inbox/{recv_ns}_{id}_{имя WAV} (контракт (13)), recv_ns —
time.time_ns(), поэтому порядок имён совпадает с порядком вызовов; id — CLAUDE_TTS_MESSAGE_ID
от демона, если это [0-9a-f]{1,64}, иначе "unknown". Запись атомарная: копия в .{имя}.part,
затем rename; плеер .part не видит. Опции (afplay -r у демона на macOS) не читаются, <wav> —
последний аргумент. Объявления демона (tts_announce.wav, tts_daemon_announce.wav) пропускаются.

Выход 1: нет аргумента, нет каталога состояния, копия не удалась; в буфере не остаётся ни
файла, ни .part. Демон не читает ни код, ни stderr, поэтому сбой идёт и в stderr, и в syslog
(journalctl -t tts-player-sink; на macOS — Console.app, поиск по tts-player-sink).
"""
import os
import re
import shutil
import sys
import syslog
import time
from pathlib import Path

from tts_player.model import UNKNOWN_TASK
from tts_player.runtime_dir import MISSING, inbox_dir, runtime_dir

ANNOUNCEMENTS = ("tts_announce.wav", "tts_daemon_announce.wav")
TASK_ID = re.compile(r"[0-9a-f]{1,64}")


def main(args: list[str]) -> int:
    if not args:
        return fail("не указан WAV-файл")
    wav = Path(args[-1])
    if wav.name in ANNOUNCEMENTS:
        return 0
    runtime = runtime_dir()
    if runtime is None:
        return fail(MISSING)
    inbox = inbox_dir(runtime)
    try:
        inbox.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return fail(f"не удалось создать буфер {inbox}: {error}")
    task_id = os.environ.get("CLAUDE_TTS_MESSAGE_ID", "")
    if not TASK_ID.fullmatch(task_id):
        task_id = UNKNOWN_TASK
    name = f"{time.time_ns()}_{task_id}_{wav.name}"
    part = inbox / f".{name}.part"
    try:
        shutil.copyfile(wav, part)
        os.replace(part, inbox / name)
    except OSError as error:
        part.unlink(missing_ok=True)
        return fail(f"не удалось положить {wav} в буфер: {error}")
    return 0


def fail(message: str) -> int:
    print(f"tts-player sink: {message}", file=sys.stderr)
    syslog.openlog("tts-player-sink")
    syslog.syslog(syslog.LOG_ERR, message)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
