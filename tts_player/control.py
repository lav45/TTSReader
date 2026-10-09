"""Забор запросов `tts-player session add/remove` из control/ (контракт (14))."""
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio  # noqa: E402

log = logging.getLogger("tts_player.control")

SUFFIX = ".json"
LEFTOVER_SUFFIX = ".tmp"


class ControlRequests:
    """Следит за control/ и выполняет запросы по порядку имён: add -> on_add(session, cwd),
    remove -> on_remove(session).

    Берутся *.json без точки в начале; *.tmp писатель ещё пишет, а его rename в .json приходит
    как RENAMED. Выполненный файл удаляется после колбэка (упавший колбэк не оставляет файл:
    иначе его подобрал бы следующий запуск повторно); битый (не JSON, не объект, неизвестный
    op, нет строкового session, add без строкового cwd) удаляется со строкой WARNING в журнал.
    Остатки *.tmp после упавшего писателя убирает start() — одним явным шагом до скана.
    """

    def __init__(self, control_dir: Path, on_add: Callable[[str, str], None],
                 on_remove: Callable[[str], None]) -> None:
        self._control_dir = Path(control_dir)
        self._on_add = on_add
        self._on_remove = on_remove
        self._monitor: Gio.FileMonitor | None = None

    def start(self) -> None:
        try:
            self._control_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            log.warning("control: каталог %s не создан: %s", self._control_dir, error)
            return
        # Сначала монитор, потом скан: файл между ними не потеряется,
        # а повторный забор того же имени пропускается.
        monitor = Gio.File.new_for_path(str(self._control_dir)).monitor_directory(
            Gio.FileMonitorFlags.WATCH_MOVES, None
        )
        monitor.connect("changed", self._on_event)
        self._monitor = monitor
        self._clean_leftovers()
        self.drain()

    def stop(self) -> None:
        if self._monitor is not None:
            self._monitor.cancel()
            self._monitor = None

    def drain(self) -> None:
        """Забрать всё, что лежит сейчас, по порядку имён, без цикла GLib."""
        try:
            with os.scandir(self._control_dir) as entries:
                names = [entry.name for entry in entries if entry.is_file()]
        except OSError as error:
            log.warning("control: каталог %s не читается: %s", self._control_dir, error)
            return
        for name in sorted(names):
            if self._wanted(name):
                self._take(name)

    def _clean_leftovers(self) -> None:
        """Остатки писателя, убитого между mkstemp и replace."""
        for stale in self._control_dir.glob("*" + LEFTOVER_SUFFIX):
            try:
                stale.unlink(missing_ok=True)
            except OSError as error:
                log.warning("control: остаток %s не удалён: %s", stale.name, error)

    def _on_event(self, monitor, file, other_file, event_type) -> None:
        if event_type == Gio.FileMonitorEvent.RENAMED and other_file is not None:
            target = other_file
        elif event_type in (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.MOVED_IN):
            target = file
        else:
            return
        name = target.get_basename()
        if self._wanted(name):
            self._take(name)

    @staticmethod
    def _wanted(name: str) -> bool:
        return name.endswith(SUFFIX) and not name.startswith(".")

    def _take(self, name: str) -> None:
        path = self._control_dir / name
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return  # уже забран (скан и событие монитора могут совпасть)
        except (OSError, UnicodeDecodeError) as error:
            log.warning("control: %s не читается (%s), удаляю", name, error)
            self._remove(path)
            return
        try:
            self._execute(name, raw)
        except Exception:
            log.exception("control: колбэк упал на %s", name)
        self._remove(path)

    def _execute(self, name: str, raw: str) -> None:
        try:
            fields = json.loads(raw)
        except ValueError:
            log.warning("control: %s не JSON, удаляю", name)
            return
        if not isinstance(fields, dict):
            log.warning("control: %s не объект, удаляю", name)
            return
        session = fields.get("session")
        if not isinstance(session, str):
            log.warning("control: в %s нет строкового session, удаляю", name)
            return
        op = fields.get("op")
        if op == "remove":
            self._on_remove(session)
            return
        if op != "add":
            log.warning("control: в %s неизвестный op %r, удаляю", name, op)
            return
        cwd = fields.get("cwd")
        if not isinstance(cwd, str):
            log.warning("control: в %s add без строкового cwd, удаляю", name)
            return
        self._on_add(session, cwd)

    @staticmethod
    def _remove(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.exception("Не удалось удалить %s", path.name)
