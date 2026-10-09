"""Запись задачи в очередь демона claude-tts: queue_dir/<время>_<id>.json."""
import logging
import secrets
import time
from pathlib import Path
from typing import Callable

from tts_player.atomic_json import write_json

log = logging.getLogger(__name__)


class DaemonQueue:
    """Кладёт задачу в каталог очереди демона. Читает его демон (glob "*.json"),
    поэтому файл появляется сразу целиком: write_json пишет во временный
    `<имя>.json.<случайное>.tmp` рядом и делает os.replace."""

    SOURCE = "tts-player"

    def __init__(
        self,
        queue_dir: Path,
        clock: Callable[[], float] = time.time,
        new_id: Callable[[], str] = lambda: secrets.token_hex(8),
    ) -> None:
        self._queue_dir = queue_dir
        self._clock = clock
        self._new_id = new_id

    def put(self, fields: dict) -> str | None:
        """Пишет задачу с новым id, timestamp и source. Возвращает id; при OSError —
        предупреждение в лог и None."""
        task_id = self._new_id()
        timestamp = self._clock()
        task = {**fields, "id": task_id, "timestamp": timestamp, "source": self.SOURCE}
        path = self._queue_dir / f"{timestamp:.6f}_{task_id}.json"
        try:
            write_json(path, task)
        except OSError as error:
            log.warning("Задача не записана в очередь демона %s: %s", path, error)
            return None
        return task_id
