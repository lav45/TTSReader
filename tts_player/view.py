"""Модель отображения: PlayerState и TextState -> то, что показывает окно. Без Gtk."""
import os
import time
from dataclasses import dataclass

from tts_player.model import Message
from tts_player.player import PlayerState
from tts_player.text_speaker import SOUNDING_STATUSES, TextState

NO_PROJECT = "—"
PENDING_MARK = "…"


@dataclass(frozen=True)
class MessageRow:
    title: str  # "HH:MM:SS  <project>"
    duration: str  # "M:SS"; пока сообщение растёт (не завершено) "M:SS…"
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
    """Время — прихода задачи хука: оно есть с появления строки и не прыгает ни с первым
    предложением, ни с переозвучкой. Без него (сообщения тестов) — первое предложение, затем
    created_at."""
    if message.received_at is not None:
        stamp = message.received_at
    elif message.sentences:
        stamp = message.sentences[0].recv_ns / 1e9
    elif message.created_at is not None:
        stamp = message.created_at
    else:
        return "--:--:--"
    return time.strftime("%H:%M:%S", time.localtime(stamp))


def _project(message: Message) -> str:
    name = os.path.basename((message.project or "").rstrip("/"))
    return name or NO_PROJECT


def _duration(complete: bool, length_s: float) -> str:
    text = format_seconds(length_s)
    return text if complete else text + PENDING_MARK


def rows(state: PlayerState) -> list[MessageRow]:
    return [
        MessageRow(
            title=f"{_start_time(m)}  {_project(m)}",
            duration=_duration(complete, length_s),
            current=m is state.current,
            heard=m.heard,
        )
        for m, length_s, complete in zip(state.messages, state.lengths, state.completes)
    ]


def _clock(position_s: float, length_s: float) -> str:
    return f"{format_seconds(position_s)} / {format_seconds(length_s)}"


def position_label(state: PlayerState) -> str:
    return _clock(state.position_s, state.length_s)


def play_pause_label(state: PlayerState) -> str:
    """Кнопка play/pause: при воспроизведении — пауза, иначе — play."""
    return "⏸" if state.status == "playing" else "⏵"


def play_pause_enabled(state: PlayerState) -> bool:
    """Нажатию есть что делать: есть текущее или непрослушанное (toggle_play_pause)."""
    return state.current is not None or any(not m.heard for m in state.messages)


def _current_index(state: PlayerState) -> int | None:
    return next((i for i, m in enumerate(state.messages) if m is state.current), None)


def prev_enabled(state: PlayerState) -> bool:
    index = _current_index(state)
    return index is not None and index > 0


def next_enabled(state: PlayerState) -> bool:
    index = _current_index(state)
    return index is not None and index < len(state.messages) - 1


def text_position_label(state: TextState) -> str:
    return _clock(state.position_s, state.length_s)


def text_play_pause_label(state: TextState) -> str:
    """Кнопка экрана «Свой текст»: звучит или вот-вот зазвучит (синтез после play) — пауза."""
    return "⏸" if state.status in SOUNDING_STATUSES else "⏵"


def text_play_enabled(state: TextState) -> bool:
    """Без текста play нечего озвучивать."""
    return state.status != "empty"
