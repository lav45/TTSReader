"""Единственный экземпляр плеера: flock на $XDG_RUNTIME_DIR/tts-player/player.lock.

Блокировку держит процесс плеера, пока жив (дескриптор открыт); в файле — его PID.
`tts-player stop` читает PID и шлёт SIGUSR1 (плеер отвечает stop_playback()).
"""
import fcntl
import os
import signal
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
    def holder_pid(runtime_dir: Path) -> int | None:
        try:
            return int(lock_path(runtime_dir).read_text().strip())
        except (OSError, ValueError):
            return None


def send_stop(runtime_dir: Path) -> bool:
    """Блокировка свободна — False; занята — SIGUSR1 владельцу и True."""
    probe = Instance.acquire(runtime_dir)
    if probe is not None:
        probe.release()
        return False
    pid = Instance.holder_pid(runtime_dir)
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGUSR1)
    except ProcessLookupError:
        return False
    return True
