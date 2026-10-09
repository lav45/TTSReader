"""Модель отображения: PlayerState -> то, что показывает окно. Без Gtk."""
import os
import time
from dataclasses import dataclass

from tts_player.model import Message
from tts_player.player import PlayerState

NO_PROJECT = "—"
PENDING_MARK = "…"


@dataclass(frozen=True)
class MessageRow:
    title: str  # "HH:MM:SS  <project>"
    duration: str  # "M:SS"; до склейки "M:SS…"
    current: bool
    heard: bool


def format_seconds(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _start_time(message: Message) -> str:
    if message.created_at is not None:
        stamp = message.created_at
    elif message.sentences:
        stamp = message.sentences[0].recv_ns / 1e9
    else:
        return "--:--:--"
    return time.strftime("%H:%M:%S", time.localtime(stamp))


def _project(message: Message) -> str:
    name = os.path.basename((message.project or "").rstrip("/"))
    return name or NO_PROJECT


def _duration(message: Message, length_s: float) -> str:
    text = format_seconds(length_s)
    return text if message.duration_s is not None else text + PENDING_MARK


def rows(state: PlayerState) -> list[MessageRow]:
    return [
        MessageRow(
            title=f"{_start_time(m)}  {_project(m)}",
            duration=_duration(m, length_s),
            current=m is state.current,
            heard=m.heard,
        )
        for m, length_s in zip(state.messages, state.lengths)
    ]


def position_label(state: PlayerState) -> str:
    return f"{format_seconds(state.position_s)} / {format_seconds(state.length_s)}"


def play_stop_label(state: PlayerState) -> str:
    return "▶" if state.status in ("stopped", "idle") else "■"


def pause_label(state: PlayerState) -> str:
    return "⏵" if state.status == "paused" else "⏸"


def current_title(state: PlayerState) -> str:
    """Заголовок окна: строка текущего сообщения; без текущего — пусто."""
    return next((row.title for row in rows(state) if row.current), "")


def pause_enabled(state: PlayerState) -> bool:
    return state.status in ("playing", "paused")


def _current_index(state: PlayerState) -> int | None:
    return next((i for i, m in enumerate(state.messages) if m is state.current), None)


def prev_enabled(state: PlayerState) -> bool:
    index = _current_index(state)
    return index is not None and index > 0


def next_enabled(state: PlayerState) -> bool:
    index = _current_index(state)
    return index is not None and index < len(state.messages) - 1
