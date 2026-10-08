"""Забор предложений из буфера inbox/ в store/ плеера."""
import logging
import os
import re
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio  # noqa: E402

from tts_player.model import Sentence  # noqa: E402

log = logging.getLogger("tts_player.inbox")

NAME_PATTERN = re.compile(
    r"^(?P<recv_ns>\d{19})_tts_queue_(?P<session_id>.+)_(?P<token>[0-9a-f]+)_s(?P<index>\d+)\.wav$"
)


class Inbox:
    """Следит за inbox/ и отдаёт предложения в on_sentence по порядку приёма.

    Файл переносится в store/ (rename на одной tmpfs) под тем же именем.
    Имена с точкой в начале (.part обёртки) не читаются.
    """

    def __init__(self, runtime_dir: Path, on_sentence: Callable[[Sentence], None]) -> None:
        base = Path(runtime_dir) / "tts-player"
        self._inbox = base / "inbox"
        self._store = base / "store"
        self._on_sentence = on_sentence
        self._monitor: Gio.FileMonitor | None = None

    def start(self) -> None:
        self._inbox.mkdir(parents=True, exist_ok=True)
        self._store.mkdir(parents=True, exist_ok=True)
        # Сначала монитор, потом скан: файл между ними не потеряется,
        # а повторный забор того же имени пропускается.
        monitor = Gio.File.new_for_path(str(self._inbox)).monitor_directory(
            Gio.FileMonitorFlags.WATCH_MOVES, None
        )
        monitor.connect("changed", self._on_event)
        self._monitor = monitor
        self._scan()

    def stop(self) -> None:
        if self._monitor is not None:
            self._monitor.cancel()
            self._monitor = None

    def _scan(self) -> None:
        names = [entry.name for entry in os.scandir(self._inbox)]
        matched = []
        for name in names:
            if name.startswith("."):
                continue
            match = NAME_PATTERN.match(name)
            if match is None:
                log.warning("Имя не подходит под шаблон предложения, остаётся в inbox: %s", name)
                continue
            matched.append((int(match["recv_ns"]), name))
        for _, name in sorted(matched):
            self._take(name)

    def _on_event(self, monitor, file, other_file, event_type) -> None:
        if event_type == Gio.FileMonitorEvent.RENAMED and other_file is not None:
            target = other_file
        elif event_type in (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.MOVED_IN):
            target = file
        else:
            return
        name = target.get_basename()
        if name.startswith("."):
            return
        if NAME_PATTERN.match(name) is None:
            log.warning("Имя не подходит под шаблон предложения, остаётся в inbox: %s", name)
            return
        self._take(name)

    def _take(self, name: str) -> None:
        match = NAME_PATTERN.match(name)
        target = self._store / name
        try:
            os.rename(self._inbox / name, target)
        except FileNotFoundError:
            return  # уже забран (скан и событие монитора могут совпасть)
        self._on_sentence(Sentence(
            recv_ns=int(match["recv_ns"]),
            session_id=match["session_id"],
            token=match["token"],
            index=int(match["index"]),
            path=target,
        ))
