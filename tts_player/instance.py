"""Единственный экземпляр плеера: flock на $XDG_RUNTIME_DIR/tts-player/player.lock.

Блокировку держит процесс плеера, пока жив (дескриптор открыт); в файле — его PID.
`tts-player` при втором запуске читает PID владельца (holder_pid); `session add|remove|has`
проверяет блокировку пробой is_locked.
"""
import fcntl
import os
import time
from pathlib import Path


def lock_path(runtime_dir: Path) -> Path:
    return runtime_dir / "tts-player" / "player.lock"


class Instance:
    def __init__(self, fd: int) -> None:
        self._fd = fd

    @classmethod
    def acquire(cls, runtime_dir: Path) -> "Instance | None":
        """Блокировка взята — экземпляр; занята другим процессом — None."""
        path = lock_path(runtime_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        return cls(fd)

    def release(self) -> None:
        os.close(self._fd)

    @staticmethod
    def holder_pid(runtime_dir: Path, timeout: float = 1.0) -> int | None:
        """PID из файла блокировки. Между flock и записью PID файл пуст или хранит чужой PID
        прошлого владельца, поэтому чтение повторяется (шаг 50 мс, до timeout)."""
        deadline = time.monotonic() + timeout
        while True:
            pid = read_pid(runtime_dir)
            if pid is not None:
                return pid
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)


def read_pid(runtime_dir: Path) -> int | None:
    try:
        return int(lock_path(runtime_dir).read_text().strip())
    except (OSError, ValueError):
        return None


def is_locked(runtime_dir: Path) -> bool:
    """Проба без записи: общая блокировка на отдельном дескрипторе, файл не меняется."""
    try:
        fd = os.open(lock_path(runtime_dir), os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False
