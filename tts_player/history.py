"""Чтение истории демона: новые склейки speech_history вместе со строкой базы."""
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

log = logging.getLogger("tts_player.history")

NAME_PATTERN = re.compile(r"^tts-.+\.wav$")
RETRY_INTERVAL_MS = 200
GIVE_UP_S = 5.0
QUERY = (
    "SELECT created_at, session_id, project, duration_seconds "
    "FROM speech_history WHERE file_name = ?"
)


@dataclass(frozen=True)
class HistoryEntry:
    path: Path  # файл склейки в speech_history (не наш)
    created_at: float
    session_id: str
    project: str
    duration_s: float


class History:
    """Сообщает в on_entry о каждой новой склейке демона (файл + строка базы).

    Демон копирует WAV в каталог (copy2, не атомарно), и только потом вставляет строку:
    файл по changes-done-hint ищется в базе, при отсутствии строки или недоступной базе
    повтор каждые 0,2 с до 5 с, затем WARNING и запись пропускается. Каждое имя
    отдаётся не более одного раза. База открывается только на чтение.
    """

    def __init__(self, history_dir: Path, db_path: Path,
                 on_entry: Callable[[HistoryEntry], None]) -> None:
        self._dir = Path(history_dir)
        self._db = Path(db_path)
        self._on_entry = on_entry
        self._monitor: Gio.FileMonitor | None = None
        self._seen: set[str] = set()
        self._timers: dict[str, int] = {}

    def start(self) -> None:
        with os.scandir(self._dir) as entries:
            self._seen = {entry.name for entry in entries}
        monitor = Gio.File.new_for_path(str(self._dir)).monitor_directory(
            Gio.FileMonitorFlags.WATCH_MOVES, None
        )
        monitor.connect("changed", self._on_event)
        self._monitor = monitor

    def stop(self) -> None:
        if self._monitor is not None:
            self._monitor.cancel()
            self._monitor = None
        for source in self._timers.values():
            GLib.source_remove(source)
        self._timers.clear()

    def _on_event(self, monitor, file, other_file, event_type) -> None:
        if event_type == Gio.FileMonitorEvent.RENAMED and other_file is not None:
            target = other_file
        elif event_type in (Gio.FileMonitorEvent.CHANGES_DONE_HINT,
                            Gio.FileMonitorEvent.MOVED_IN):
            target = file
        else:
            return
        name = target.get_basename()
        if name.startswith(".") or NAME_PATTERN.match(name) is None:
            return
        if name in self._seen:
            return
        self._seen.add(name)
        deadline = time.monotonic() + GIVE_UP_S
        if self._attempt(name, deadline):
            self._timers[name] = GLib.timeout_add(RETRY_INTERVAL_MS, self._retry, name, deadline)

    def _retry(self, name: str, deadline: float) -> bool:
        if not self._attempt(name, deadline):
            self._timers.pop(name, None)
            return False
        return True

    def _attempt(self, name: str, deadline: float) -> bool:
        """Одна попытка чтения. True — попробовать ещё; False — закончено (доставлено или сдались)."""
        try:
            row = self._lookup(name)
        except (sqlite3.Error, OSError) as error:
            reason = f"база недоступна: {error}"
        else:
            if row is not None:
                self._deliver(name, row)
                return False
            reason = "строки в speech_history нет"
        if time.monotonic() >= deadline:
            log.warning("Склейка %s без записи истории дольше %.0f с (%s), пропускаю",
                        name, GIVE_UP_S, reason)
            return False
        return True

    def _lookup(self, name: str):
        conn = sqlite3.connect(f"file:{self._db}?mode=ro", uri=True)
        try:
            return conn.execute(QUERY, (name,)).fetchone()
        finally:
            conn.close()

    def _deliver(self, name: str, row) -> None:
        created_at, session_id, project, duration = row
        entry = HistoryEntry(
            path=self._dir / name,
            created_at=created_at,
            session_id=session_id or "",
            project=project or "",
            duration_s=duration or 0.0,
        )
        try:
            self._on_entry(entry)
        except Exception:
            log.exception("on_entry упал на %s", name)
