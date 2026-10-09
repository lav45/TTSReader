"""Модель данных плеера."""
from dataclasses import dataclass
from pathlib import Path

UNKNOWN_TASK = "unknown"
"""id задачи, когда обёртка paplay не нашла текущую задачу демона (контракт (13))."""


@dataclass(frozen=True)
class Sentence:
    """Предложение, уже перенесённое в store/.

    task_id — id задачи демона из имени файла (обёртка paplay) или UNKNOWN_TASK;
    token — проход синтеза: повторный проход одной задачи получает новый token.
    """
    recv_ns: int
    task_id: str
    session_id: str
    token: str
    index: int
    path: Path


@dataclass(eq=False)
class Message:
    """Сообщение: предложения одной задачи демона по возрастанию recv_ns.

    Задача — task_id; при UNKNOWN_TASK — пара (session_id, token), как до id задачи.
    token — первого прохода синтеза, вошедшего в сообщение (для журнала).
    Сравнивается по идентичности: два объекта — два разных сообщения очереди.
    audio — копия склейки демона в store/ (None, пока её нет или длительности
    разошлись); created_at/project/duration_s — из записи истории, до неё None.
    heard — сообщение прослушано (MessageQueue.mark_heard): его поздние предложения
    не добавляются, release() его файлы удаляет.
    """
    task_id: str
    session_id: str
    token: str
    sentences: list[Sentence]
    audio: Path | None = None
    created_at: float | None = None
    project: str | None = None
    duration_s: float | None = None
    heard: bool = False
