"""Подменённый HOME для хука claude-tts и heartbeat, отражающий жизнь настоящего демона.

`claude-tts speak --from-hook` запускается с HOME=<home> и пишет задачу в
<home>/.claude-tts/queue; его проверка daemon_healthy() смотрит на
<home>/.claude-tts/daemon.heartbeat. HookHome держит этот файл свежим, пока жив
настоящий демон (по его heartbeat или PID в tts_root). Ошибки ввода-вывода —
предупреждение в лог (stderr), наружу исключение не выходит."""
import logging
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

log = logging.getLogger(__name__)

HEARTBEAT = "daemon.heartbeat"
PID_FILE = "daemon.pid"
LINKED = ("config.json", "sessions.d")


class HookHome:
    """Каталог hook-home и таймер heartbeat."""

    BEAT_SECONDS = 10
    FRESH_SECONDS = 30

    def __init__(self, runtime_dir: Path, tts_root: Path,
                 clock: Callable[[], float] = time.time) -> None:
        self._tts_root = tts_root
        self._clock = clock
        self._timer_id: int | None = None
        self.home = runtime_dir / "hook-home"
        self._base = self.home / ".claude-tts"
        self.queue_dir = self._base / "queue"

    def prepare(self) -> None:
        """Создаёт каталоги и ссылки, чистит остатки; каждый шаг в своём try.

        Сбой одного шага (например, настоящий каталог на месте ссылки) не пропускает остальные."""
        self._step("каталог queue", lambda: self.queue_dir.mkdir(parents=True, exist_ok=True))
        # sessions.d хук создаёт командой mkdir; на висячей ссылке она не сработает
        self._step("sessions.d", lambda: (self._tts_root / "sessions.d").mkdir(
            parents=True, exist_ok=True))
        for name in LINKED:
            self._step(f"ссылка {name}", lambda name=name: self._link(
                self._base / name, self._tts_root / name))
        self._step("очистка queue", self._clean_queue)
        self._step("очистка heartbeat", self._clean_heartbeat_tmp)

    def _step(self, what: str, action: Callable[[], object]) -> None:
        try:
            action()
        except OSError as e:
            log.warning("hook-home: %s (%s): %s", what, self.home, e)

    def _clean_queue(self) -> None:
        for stale in (*self.queue_dir.glob("*.json"), *self.queue_dir.glob("*.tmp")):
            stale.unlink(missing_ok=True)

    def _clean_heartbeat_tmp(self) -> None:
        """Остатки _write_text после падения процесса между mkstemp и replace."""
        for stale in self._base.glob(HEARTBEAT + ".*.tmp"):
            stale.unlink(missing_ok=True)

    def beat(self) -> bool:
        """Один шаг: True — heartbeat записан, False — удалён (или не записан из-за ошибки)."""
        target = self._base / HEARTBEAT
        try:
            if self._daemon_alive():
                self._write_text(target, str(self._clock()))
                return True
            target.unlink(missing_ok=True)
        except OSError as e:
            log.warning("hook-home: heartbeat %s: %s", target, e)
        return False

    def start(self) -> None:
        self.prepare()
        self.beat()
        if self._timer_id is None:
            self._timer_id = GLib.timeout_add_seconds(self.BEAT_SECONDS, self._on_timer)

    def stop(self) -> None:
        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
            self._timer_id = None
        try:
            (self._base / HEARTBEAT).unlink(missing_ok=True)
        except OSError as e:
            log.warning("hook-home: heartbeat не удалён: %s", e)

    def _on_timer(self) -> bool:
        self.beat()
        return True

    def _daemon_alive(self) -> bool:
        return self._heartbeat_fresh() or self._pid_alive()

    def _heartbeat_fresh(self) -> bool:
        try:
            last = float((self._tts_root / HEARTBEAT).read_text().strip())
        except (OSError, ValueError):
            return False
        return self._clock() - last <= self.FRESH_SECONDS

    def _pid_alive(self) -> bool:
        try:
            pid = int((self._tts_root / PID_FILE).read_text().strip())
            os.kill(pid, 0)
        except (OSError, ValueError, OverflowError):  # как daemon_healthy: PermissionError — тоже мёртв
            return False
        return True

    @staticmethod
    def _link(link: Path, target: Path) -> None:
        if link.is_symlink() or link.is_file():
            link.unlink()
        link.symlink_to(target)

    @staticmethod
    def _write_text(path: Path, text: str) -> None:
        """Временный файл рядом + os.replace; при сбое временный убирается."""
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(tmp_name, 0o644)
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
