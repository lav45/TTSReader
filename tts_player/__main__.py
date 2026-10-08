"""Процесс плеера: Inbox, History -> MessageQueue -> Playback.

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

from tts_player.history import History, HistoryEntry  # noqa: E402
from tts_player.inbox import Inbox  # noqa: E402
from tts_player.model import Message, Sentence  # noqa: E402
from tts_player.playback import Playback  # noqa: E402
from tts_player.queue import MessageQueue  # noqa: E402

log = logging.getLogger("tts_player")


class Player:
    """Связка частей: пока ничего не играет (idle), новое предложение запускается сразу.

    Ошибка файла — пропуск. Отказ вывода звука — то же место через RETRY_SECONDS,
    строка error — одна на отказ, а не на каждый повтор.

    Склейка играющего по предложениям сообщения подменяет их с той же позиции:
    начало текущего предложения в сообщении плюс позиция в нём. Пока вывод недоступен,
    позиции нет — повтор начнёт склейку с начала текущего предложения.
    """
    RETRY_SECONDS = 2

    def __init__(self, runtime_dir: Path, audio_sink: str | None,
                 history_dir: Path, history_db: Path) -> None:
        self._queue = MessageQueue()
        self._playback = Playback(audio_sink)
        self._playback.on_finished = self._on_finished
        self._playback.on_error = self._on_error
        self._inbox = Inbox(runtime_dir, self._on_sentence)
        self._history = History(history_dir, history_db, self._on_entry)
        self._history_dir = history_dir
        self._idle = True
        self._output_down = False
        self._retry_id = 0
        self._sentence: Sentence | None = None
        self._target: tuple[Path, float] | None = None

    def start(self) -> None:
        """История — раньше inbox/: накопленное в inbox/ начинает играть уже в start(),
        и склейка, записанная после первой строки playing, не должна считаться
        существовавшей до запуска. Колбэки обеих частей идут из цикла GLib, поэтому
        такой порядок ничего не теряет."""
        try:
            self._history.start()
        except OSError as error:
            log.warning("история %s недоступна (%s), работаю без склеек",
                        self._history_dir, error)
        self._inbox.start()

    def stop(self) -> None:
        """Штатный выход: незаконченное — обратно в inbox/, законченное удаляет очередь."""
        self._inbox.stop()
        self._history.stop()
        self._playback.stop()
        if self._retry_id:
            GLib.source_remove(self._retry_id)
            self._retry_id = 0
        self._inbox.put_back(self._queue.release())

    def _on_sentence(self, sentence: Sentence) -> None:
        self._queue.add(sentence)
        if self._idle:
            self._play_next()

    def _on_entry(self, entry: HistoryEntry) -> None:
        message = self._queue.attach(entry, self._inbox.store)
        if message is None:
            return
        if message.audio is None:
            log.info("ended %s %s", message.session_id, message.token)
            return
        log.info("complete %s %s %.2fs", message.session_id, message.token,
                 message.duration_s)
        sentence = self._sentence
        if sentence is None or message is not self._queue.current:
            return
        position = self._queue.offset_of(message, sentence) + (self._playback.position() or 0.0)
        log.info("switched %s at %.2f", message.audio.name, position)
        self._sentence = None
        self._target = (message.audio, position)
        if not self._output_down:
            self._playback.play(message.audio, start_s=position)

    def _on_finished(self, path: Path) -> None:
        self._output_down = False
        self._play_next()

    def _on_error(self, path: Path, text: str, output_failed: bool) -> None:
        if not output_failed:
            self._output_down = False
            log.info("error %s: %s", path.name, text)
            self._play_next()
            return
        if not self._output_down:
            log.info("error %s: %s", path.name, text)
        self._output_down = True
        self._retry_id = GLib.timeout_add_seconds(self.RETRY_SECONDS, self._retry)

    def _retry(self) -> bool:
        self._retry_id = 0
        # Повтор ставится только из on_error играющего файла, а _target сбрасывается
        # лишь в _play_next без элемента — пока повтор ждёт, _play_next не зовётся.
        assert self._target is not None, "повтор без играющего файла"
        path, start_s = self._target
        self._playback.play(path, start_s=start_s)
        return GLib.SOURCE_REMOVE

    def _play_next(self) -> None:
        item = self._queue.next_item()
        self._idle = item is None
        self._sentence = item if isinstance(item, Sentence) else None
        if item is None:
            self._target = None
            return
        path = item.audio if isinstance(item, Message) else item.path
        self._target = (path, 0.0)
        log.info("playing %s", path.name)
        self._playback.play(path)


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
