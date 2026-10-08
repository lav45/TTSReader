"""Модель данных плеера."""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Sentence:
    """Предложение, уже перенесённое в store/."""
    recv_ns: int
    session_id: str
    token: str
    index: int
    path: Path


@dataclass(eq=False)
class Message:
    """Сообщение: предложения одного (session_id, token) по возрастанию recv_ns.

    Сравнивается по идентичности: два объекта — два разных сообщения очереди.
    audio — копия склейки демона в store/ (None, пока её нет или длительности
    разошлись); created_at/project/duration_s — из записи истории, до неё None.
    heard — сообщение прослушано (MessageQueue.mark_heard): его поздние предложения
    не добавляются, release() его файлы удаляет.
    """
    session_id: str
    token: str
    sentences: list[Sentence]
    audio: Path | None = None
    created_at: float | None = None
    project: str | None = None
    duration_s: float | None = None
    heard: bool = False
