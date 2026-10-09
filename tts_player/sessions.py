"""Реестр сессий Claude Code, о которых знает плеер, и файл sessions.json (контракт 15).

Файл $XDG_RUNTIME_DIR/tts-player/sessions.json пишет только плеер, атомарно:
{"active": "<uuid>" | null, "sessions": [{"id": "<uuid>", "project": "<последний компонент cwd>"}, …]}.
Читает его CLI (`tts-player session has`) через read_sessions."""
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from tts_player import atomic_json

log = logging.getLogger(__name__)

APP_DIR = "tts-player"
FILE_NAME = "sessions.json"


@dataclass(frozen=True)
class Session:
    id: str
    project: str


def sessions_path(runtime_dir: Path) -> Path:
    return Path(runtime_dir) / APP_DIR / FILE_NAME


def read_sessions(path: Path) -> tuple[str | None, list[Session]]:
    """(active, sessions). Нет файла — (None, []); нечитаемый или неверной формы — ValueError."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, []
    except OSError as e:
        raise ValueError(f"не прочитан {path}: {e.strerror or e}") from e
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ValueError(f"{path} не является корректным JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path}: ожидался JSON-объект")
    raw = data.get("sessions")
    if not isinstance(raw, list):
        raise ValueError(f"{path}: sessions не список")
    sessions = []
    for item in raw:
        if not (isinstance(item, dict)
                and isinstance(item.get("id"), str) and isinstance(item.get("project"), str)):
            raise ValueError(f"{path}: элемент sessions {item!r} без строковых id и project")
        sessions.append(Session(item["id"], item["project"]))
    active = data.get("active")
    if active is not None and active not in {s.id for s in sessions}:
        raise ValueError(f"{path}: active {active!r} нет в sessions")
    return active, sessions


class Sessions:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._sessions: list[Session] = []
        self._active: str | None = None

    @property
    def sessions(self) -> list[Session]:
        return list(self._sessions)

    @property
    def active(self) -> str | None:
        return self._active

    def has(self, session_id: str) -> bool:
        return any(s.id == session_id for s in self._sessions)

    def load(self) -> None:
        """Поднимает файл прошлого запуска; битый — пустой реестр и WARNING."""
        try:
            self._active, self._sessions = read_sessions(self._path)
        except ValueError as e:
            log.warning("sessions.json не принят, реестр пуст: %s", e)
            self._active, self._sessions = None, []

    def add(self, session_id: str, cwd: str) -> bool:
        """False — сессия уже в списке, файл не пишется. Первая сессия становится активной."""
        if self.has(session_id):
            return False
        self._sessions.append(Session(session_id, Path(cwd).name))
        if self._active is None:
            self._active = session_id
        self._write()
        return True

    def remove(self, session_id: str) -> bool:
        if not self.has(session_id):
            return False
        self._sessions = [s for s in self._sessions if s.id != session_id]
        if self._active == session_id:
            self._active = None
        self._write()
        return True

    def _write(self) -> None:
        data = {
            "active": self._active,
            "sessions": [{"id": s.id, "project": s.project} for s in self._sessions],
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json.write_json(self._path, data)
        except OSError as e:
            log.warning("Не удалось записать %s: %s", self._path, e.strerror or e)
