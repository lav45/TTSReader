"""Озвучка сессии Claude Code: включена ли и выключение через хук /tts-off.

Озвучка включена, пока в флаге (/tmp/claude-tts.on) лежит ID озвучиваемой сессии.
Выключает её хук tts-toggle.sh off: он сверяет CLAUDE_CODE_SESSION_ID с флагом,
поэтому плеер подставляет ID из самого флага. Сообщения идут через logging
(в stderr плеера, а при запуске из tts-player-hook — в player.log).
"""
import logging
import os
import subprocess
from pathlib import Path

from gi.repository import GLib

log = logging.getLogger(__name__)


class Voicing:
    def __init__(self, flag: Path, hook: Path) -> None:
        self._flag = Path(flag)
        self._hook = Path(hook)

    def is_on(self) -> bool:
        """Флаг существует и непуст после strip()."""
        return self._session() != ""

    def turn_off(self) -> None:
        """Запускает `<hook> off` для озвучиваемой сессии и сразу возвращается.

        Код возврата ≠ 0 попадает в лог из главного цикла GLib; сбой запуска —
        тоже в лог, исключение наружу не выходит.
        """
        session = self._session()
        if session == "":
            log.info("озвучка уже выключена")
            return
        env = {**os.environ, "CLAUDE_CODE_SESSION_ID": session}
        try:
            process = subprocess.Popen(
                [str(self._hook), "off"], env=env, start_new_session=True
            )
        except OSError as error:
            log.warning("не удалось запустить хук %s: %s", self._hook, error)
            return
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, process.pid, self._on_exit, process)

    def _session(self) -> str:
        try:
            return self._flag.read_text().strip()
        except OSError:
            return ""

    def _on_exit(self, pid: int, status: int, process: subprocess.Popen) -> None:
        process.poll()  # GLib уже собрал процесс; закрывает учёт Popen без ResourceWarning
        try:
            GLib.spawn_check_wait_status(status)
        except GLib.Error as error:
            log.warning("хук %s off завершился с ошибкой: %s", self._hook, error.message)
