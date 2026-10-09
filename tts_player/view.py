"""Модель отображения: PlayerState и TextState -> то, что показывает окно. Без Gtk."""
import os
import time
from dataclasses import dataclass

from tts_player.model import Message
from tts_player.player import PlayerState
from tts_player.text_speaker import SOUNDING_STATUSES, TextState

NO_PROJECT = "—"
TITLE_WORDS = 8
CUT_MARK = "…"


@dataclass(frozen=True)
class MessageRow:
    """Дорожка внутри открытой папки сессии."""
    title: str  # первые слова текста; без текста — проект
    time: str  # "HH:MM:SS" прихода
    duration: str  # "M:SS"
    current: bool
    heard: bool
    index: int  # место сообщения в PlayerState.messages


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


def _subject(message: Message) -> str:
    """Первые TITLE_WORDS слов озвученного текста одной строкой; без текста — проект."""
    words = (message.text or "").split()
    if not words:
        return _project(message)
    subject = " ".join(words[:TITLE_WORDS])
    return subject + CUT_MARK if len(words) > TITLE_WORDS else subject


@dataclass(frozen=True)
class SessionRow:
    """Заголовок папки сессии: открыта — активна."""
    title: str  # "<project> · <id[:8]>"; project пуст — NO_PROJECT
    count: str  # число дорожек сессии
    open: bool
    id: str


def rows(state: PlayerState) -> list[MessageRow]:
    """Дорожки открытой сессии (сообщения состояния) по порядку."""
    return [
        MessageRow(
            title=_subject(m),
            time=_start_time(m),
            duration=format_seconds(length_s),
            current=m is state.current,
            heard=m.heard,
            index=index,
        )
        for index, (m, length_s) in enumerate(zip(state.messages, state.lengths))
    ]


def session_rows(state: PlayerState) -> list[SessionRow]:
    return [
        SessionRow(
            title=f"{session.project or NO_PROJECT} · {session.id[:8]}",
            count=str(count),
            open=session.id == state.active,
            id=session.id,
        )
        for session, count in zip(state.sessions, state.counts)
    ]


def tree_rows(state: PlayerState) -> list[SessionRow | MessageRow]:
    """Папки сессий по порядку списка; дорожки — сразу за заголовком открытой."""
    tree: list[SessionRow | MessageRow] = []
    for folder in session_rows(state):
        tree.append(folder)
        if folder.open:
            tree += rows(state)
    return tree


def clear_enabled(state: PlayerState) -> bool:
    """Мусорка очищает активную сессию: без неё ей нечего делать."""
    return state.active is not None


def _clock(position_s: float, length_s: float) -> str:
    return f"{format_seconds(position_s)} / {format_seconds(length_s)}"


def position_label(state: PlayerState) -> str:
    return _clock(state.position_s, state.length_s)


def play_pause_label(state: PlayerState) -> str:
    """Кнопка play/pause: при воспроизведении — пауза, иначе — play."""
    return "⏸" if state.status == "playing" else "⏵"


def play_pause_enabled(state: PlayerState) -> bool:
    """Нажатию есть что делать: есть текущее или хоть одно сообщение — без текущего
    играет последнее (toggle_play_pause)."""
    return state.current is not None or bool(state.messages)


def text_position_label(state: TextState) -> str:
    return _clock(state.position_s, state.length_s)


def text_play_pause_label(state: TextState) -> str:
    """Кнопка экрана «Свой текст»: звучит или вот-вот зазвучит (синтез после play) — пауза."""
    return "⏸" if state.status in SOUNDING_STATUSES else "⏵"


def text_play_enabled(state: TextState) -> bool:
    """Без текста play нечего озвучивать."""
    return state.status != "empty"
