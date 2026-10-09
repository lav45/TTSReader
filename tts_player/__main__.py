"""Процесс плеера: разбор аргументов и главный цикл; устройство — в tts_player.player.

Запуск: python3 -m tts_player [--audio-sink NAME] [--history-dir DIR] [--history-db FILE].
"""
import argparse
import logging
import os
import signal
import sys
from pathlib import Path

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from tts_player.player import Player  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tts_player")
    parser.add_argument("--audio-sink", default=None, help="элемент GStreamer для вывода звука")
    parser.add_argument("--history-dir", default="~/.claude-tts/speech_history",
                        help="каталог склеек демона")
    parser.add_argument("--history-db", default="~/.claude-tts/handy_analysis.db",
                        help="база истории демона (только чтение)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        print("tts_player: XDG_RUNTIME_DIR не задан", file=sys.stderr)
        return 1

    loop = GLib.MainLoop()
    player = Player(Path(runtime), args.audio_sink,
                    Path(args.history_dir).expanduser(), Path(args.history_db).expanduser())

    def quit_loop() -> bool:
        loop.quit()
        return GLib.SOURCE_REMOVE

    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, quit_loop)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, quit_loop)
    player.start()
    try:
        loop.run()
    finally:
        player.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
