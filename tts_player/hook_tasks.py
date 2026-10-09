"""Забор задач хука claude-tts из hook-home/.claude-tts/queue/ (контракт (11))."""
import json
import logging
from collections.abc import Callable
from pathlib import Path

from tts_player.mailbox import Mailbox
from tts_player.model import HookTask

log = logging.getLogger("tts_player.hook_tasks")


class HookTasks:
    """Следит за queue/ (Mailbox) и отдаёт задачи в on_task по порядку имён.

    Битый файл (не JSON, не объект, нет строковых id и text; негодные timestamp и speed —
    значения по умолчанию) удаляется со строкой в журнал (stderr).
    """

    def __init__(self, queue_dir: Path, on_task: Callable[[HookTask], None]) -> None:
        self._on_task = on_task
        self._mailbox = Mailbox(queue_dir, self._handle, log, "hook-tasks")

    def start(self) -> None:
        self._mailbox.start()

    def stop(self) -> None:
        self._mailbox.stop()

    def _handle(self, name: str, raw: str) -> None:
        task = self._parse(name, raw)
        if task is not None:
            self._on_task(task)

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
        timestamp = HookTasks._number(name, fields, "timestamp", 0.0)
        speed = HookTasks._number(name, fields, "speed", 1.0)
        return HookTask(task_id, timestamp, str(fields.get("session_id", "")),
                        str(fields.get("project", "")), text,
                        str(fields.get("persona", "")), speed, fields)

    @staticmethod
    def _number(name: str, fields: dict, key: str, default: float) -> float:
        """Число из поля; отсутствует — default молча, не число — default с предупреждением."""
        if key not in fields:
            return default
        try:
            return float(fields[key])
        except (TypeError, ValueError):
            log.warning("hook-tasks: в %s %s не число, беру %s", name, key, default)
            return default
