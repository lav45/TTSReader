"""Подкоманда `tts-player session add|remove|has`: гейт хука Claude Code зовёт её на каждом хуке,
поэтому модуль не импортирует плеер и gi — только instance, sessions и стандартную библиотеку.

add и remove не трогают sessions.json: кладут запрос в $XDG_RUNTIME_DIR/tts-player/control/<ns>.json
(atomic_json.write_json: временный *.tmp рядом, затем rename; сбой убирает его); список меняет плеер. has только читает sessions.json.
"""
import os
import sys
import time
from pathlib import Path

from tts_player.atomic_json import write_json
from tts_player.instance import is_locked
from tts_player.sessions import control_dir, read_sessions, sessions_path

SESSION_USAGE = "usage: tts-player session add <uuid> <cwd> | remove <uuid> | has <uuid>"


def main(args: list[str]) -> int:
    if len(args) == 3 and args[0] == "add":
        return session_add(args[1], args[2])
    if len(args) == 2 and args[0] == "remove":
        return session_remove(args[1])
    if len(args) == 2 and args[0] == "has":
        return session_has(args[1])
    print(SESSION_USAGE, file=sys.stderr)
    return 2


def runtime_dir() -> Path | None:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return Path(runtime) if runtime else None


def session_has(session_id: str) -> int:
    """0 — UUID в sessions.json запущенного плеера; иначе 1 (нет runtime-каталога, плеера,
    файла, битый файл, нет UUID). Ничего не создаёт и не пишет."""
    runtime = runtime_dir()
    if runtime is None or not is_locked(runtime):
        return 1
    try:
        _active, sessions = read_sessions(sessions_path(runtime))
    except ValueError:
        return 1
    return 0 if any(s.id == session_id for s in sessions) else 1


def is_listed(runtime: Path, session_id: str) -> bool:
    """В sessions.json; нет файла или он битый — список пуст."""
    try:
        _active, sessions = read_sessions(sessions_path(runtime))
    except ValueError:
        return False
    return any(s.id == session_id for s in sessions)


def session_add(session_id: str, cwd: str) -> int:
    request = {"op": "add", "session": session_id, "cwd": cwd}
    return submit(session_id, request, noop_text="сессия уже в списке", sent_text="сессия передана плееру",
                  offline_text="плеер не запущен — сессия добавится при его запуске", noop_when_listed=True)


def session_remove(session_id: str) -> int:
    request = {"op": "remove", "session": session_id}
    return submit(session_id, request, noop_text="сессия не в списке", sent_text="сессия передана плееру на удаление",
                  offline_text="плеер не запущен — сессия уберётся при его запуске", noop_when_listed=False)


def submit(session_id: str, request: dict, noop_text: str, sent_text: str, offline_text: str,
           noop_when_listed: bool) -> int:
    """Запрос не нужен, если список уже в нужном состоянии: add — UUID есть, remove — UUID нет."""
    runtime = runtime_dir()
    if runtime is None:
        print("tts-player: XDG_RUNTIME_DIR не задан", file=sys.stderr)
        return 1
    if is_listed(runtime, session_id) == noop_when_listed:
        print(noop_text)
        return 0
    try:
        write_request(runtime, request)
    except OSError as e:
        print(f"tts-player: запрос не записан: {e}", file=sys.stderr)
        return 1
    print(sent_text if is_locked(runtime) else offline_text)
    return 0


def write_request(runtime: Path, request: dict) -> None:
    control = control_dir(runtime)
    control.mkdir(parents=True, exist_ok=True)
    write_json(control / f"{time.time_ns()}.json", request)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
