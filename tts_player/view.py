"""Модель отображения: PlayerState -> то, что показывает окно. Без Gtk."""
import os
import time
import wave
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

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


@lru_cache(maxsize=4096)
def _wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / w.getframerate()
    except (OSError, EOFError, wave.Error, ZeroDivisionError):
        return 0.0


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


def _duration(message: Message) -> str:
    if message.duration_s is not None:
        return format_seconds(message.duration_s)
    total = sum(_wav_seconds(s.path) for s in message.sentences)
    return format_seconds(total) + PENDING_MARK


def rows(state: PlayerState) -> list[MessageRow]:
    return [
        MessageRow(
            title=f"{_start_time(m)}  {_project(m)}",
            duration=_duration(m),
            current=m is state.current,
            heard=m.heard,
        )
        for m in state.messages
    ]


def position_label(state: PlayerState) -> str:
    return f"{format_seconds(state.position_s)} / {format_seconds(state.length_s)}"


def play_stop_label(state: PlayerState) -> str:
    return "▶" if state.status in ("stopped", "idle") else "■"


def pause_label(state: PlayerState) -> str:
    return "⏵" if state.status == "paused" else "⏸"
