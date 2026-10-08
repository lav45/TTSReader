"""Процесс плеера: Inbox -> MessageQueue -> Playback. Запуск: python3 -m tts_player [--audio-sink NAME]."""
import argparse
import logging
import os
import signal
import sys
from pathlib import Path

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from tts_player.inbox import Inbox  # noqa: E402
from tts_player.model import Sentence  # noqa: E402
from tts_player.playback import Playback  # noqa: E402
from tts_player.queue import MessageQueue  # noqa: E402

log = logging.getLogger("tts_player")


class Player:
    """Связка трёх частей: пока ничего не играет (idle), новое предложение запускается сразу."""

    def __init__(self, runtime_dir: Path, audio_sink: str | None) -> None:
        self._queue = MessageQueue()
        self._playback = Playback(audio_sink)
        self._playback.on_finished = self._on_finished
        self._playback.on_error = self._on_error
        self._inbox = Inbox(runtime_dir, self._on_sentence)
        self._idle = True

    def start(self) -> None:
        self._inbox.start()

    def stop(self) -> None:
        self._inbox.stop()
        self._playback.stop()

    def _on_sentence(self, sentence: Sentence) -> None:
        self._queue.add(sentence)
        if self._idle:
            self._play_next()

    def _on_finished(self, path: Path) -> None:
        self._play_next()

    def _on_error(self, path: Path, text: str) -> None:
        log.info("error %s: %s", path.name, text)
        self._play_next()

    def _play_next(self) -> None:
        sentence = self._queue.next_sentence()
        self._idle = sentence is None
        if sentence is None:
            return
        log.info("playing %s", sentence.path.name)
        self._playback.play(sentence.path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tts_player")
    parser.add_argument("--audio-sink", default=None, help="элемент GStreamer для вывода звука")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        print("tts_player: XDG_RUNTIME_DIR не задан", file=sys.stderr)
        return 1

    loop = GLib.MainLoop()
    player = Player(Path(runtime), args.audio_sink)

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
