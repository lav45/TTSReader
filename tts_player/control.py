"""Забор запросов `tts-player session add/remove` из control/ (контракт (14))."""
import json
import logging
from collections.abc import Callable
from pathlib import Path

from tts_player.mailbox import Mailbox

log = logging.getLogger("tts_player.control")


class ControlRequests:
    """Следит за control/ и выполняет запросы по порядку имён: add -> on_add(session, cwd),
    remove -> on_remove(session).

    Забор файлов — Mailbox; здесь разбор: битый запрос (не JSON, не объект, неизвестный op,
    нет строкового session, add без строкового cwd) удаляется со строкой WARNING в журнал,
    колбэк не зван. Остатки *.tmp после упавшего писателя убирает start().
    """

    def __init__(self, control_dir: Path, on_add: Callable[[str, str], None],
                 on_remove: Callable[[str], None]) -> None:
        self._on_add = on_add
        self._on_remove = on_remove
        self._mailbox = Mailbox(control_dir, self._handle, log, "control", clean_leftovers=True)

    def start(self) -> None:
        self._mailbox.start()

    def stop(self) -> None:
        self._mailbox.stop()

    def drain(self) -> None:
        """Забрать всё, что лежит сейчас, по порядку имён, без цикла GLib."""
        self._mailbox.drain()

    def _handle(self, name: str, raw: str) -> None:
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
