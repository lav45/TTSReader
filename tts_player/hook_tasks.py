"""Забор задач хука claude-tts из hook-home/.claude-tts/queue/ (контракт (11))."""
import json
import logging
import os
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio  # noqa: E402

from tts_player.model import HookTask  # noqa: E402

log = logging.getLogger("tts_player.hook_tasks")

SUFFIX = ".json"


class HookTasks:
    """Следит за queue/ и отдаёт задачи в on_task по порядку имён.

    Берутся *.json без точки в начале; *.tmp хук ещё пишет, а его rename в .json приходит
    как RENAMED. Разобранный файл удаляется после on_task (упавший on_task не оставляет
    файл: иначе его подобрал бы следующий запуск повторно); битый (не JSON, не объект,
    нет строковых id и text) удаляется со строкой в журнал (stderr).
    """

    def __init__(self, queue_dir: Path, on_task: Callable[[HookTask], None]) -> None:
        self._queue_dir = Path(queue_dir)
        self._on_task = on_task
        self._monitor: Gio.FileMonitor | None = None

    def start(self) -> None:
        self._queue_dir.mkdir(parents=True, exist_ok=True)
        # Сначала монитор, потом скан: файл между ними не потеряется,
        # а повторный забор того же имени пропускается.
        monitor = Gio.File.new_for_path(str(self._queue_dir)).monitor_directory(
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
        with os.scandir(self._queue_dir) as entries:
            names = [entry.name for entry in entries if entry.is_file()]
        for name in sorted(names):
            if self._wanted(name):
                self._take(name)

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
        path = self._queue_dir / name
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return  # уже забран (скан и событие монитора могут совпасть)
        except (OSError, UnicodeDecodeError) as error:
            log.warning("hook-tasks: %s не читается (%s), удаляю", name, error)
            self._remove(path)
            return
        task = self._parse(name, raw)
        if task is not None:
            try:
                self._on_task(task)
            except Exception:
                log.exception("on_task упал на %s", name)
        self._remove(path)

    @staticmethod
    def _parse(name: str, raw: str) -> HookTask | None:
        try:
            fields = json.loads(raw)
        except ValueError:
            log.warning("hook-tasks: %s не JSON, удаляю", name)
            return None
        if not isinstance(fields, dict):
            log.warning("hook-tasks: %s не объект, удаляю", name)
            return None
        task_id = fields.get("id")
        text = fields.get("text")
        if not isinstance(task_id, str) or not isinstance(text, str):
            log.warning("hook-tasks: в %s нет строковых id и text, удаляю", name)
            return None
        try:
            timestamp = float(fields.get("timestamp", 0.0))
            speed = float(fields.get("speed", 1.0))
        except (TypeError, ValueError):
            log.warning("hook-tasks: в %s timestamp или speed не число, удаляю", name)
            return None
        return HookTask(task_id, timestamp, str(fields.get("session_id", "")),
                        str(fields.get("project", "")), text,
                        str(fields.get("persona", "")), speed, fields)

    @staticmethod
    def _remove(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.exception("Не удалось удалить %s", path.name)
