"""Модель данных плеера."""
from dataclasses import dataclass, field
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
    """Сообщение: текст задачи хука и предложения его задач демона по возрастанию recv_ns.

    Появляется с задачей хука (контракт (11)), ещё без предложений. task_ids — id задач
    демона, поставленных для него (контракт (12)): исходной и переозвучек; предложение с
    одним из них — его (контракт (13)). sent — последняя задача ушла в демон; False — запись
    не удалась или текст отложен, ждать предложений нечего. deferred — текст ещё не отдан
    демону: сессия была неактивна или звучал свой текст; отдаётся при начале проигрывания,
    флаг снимается с поставленной задачей. persona/speed — голос, которым озвучен (или
    озвучивается) кэш: сравниваются с текущими при начале проигрывания. received_at — время
    задачи хука, по нему строка списка. Сравнивается по идентичности: два объекта — два
    разных сообщения очереди.
    audio — копия склейки демона в store/ (None, пока её нет или длительности
    разошлись); created_at/duration_s — из записи истории, до неё None; project — из задачи
    хука, запись истории его подтверждает.
    heard — сообщение прослушано (MessageQueue.mark_heard): его поздние предложения
    не добавляются.
    """
    session_id: str
    project: str | None = None
    text: str | None = None
    persona: str | None = None
    speed: float | None = None
    task_ids: set[str] = field(default_factory=set)
    sentences: list[Sentence] = field(default_factory=list)
    received_at: float | None = None
    sent: bool = True
    audio: Path | None = None
    created_at: float | None = None
    duration_s: float | None = None
    heard: bool = False
    deferred: bool = False


@dataclass(frozen=True)
class HookTask:
    """Задача хука claude-tts из hook-home/.claude-tts/queue/ (контракт (11)).

    fields — вся задача как есть: из неё берут остальное (голос, метод скорости и т.д.)."""
    id: str
    timestamp: float
    session_id: str
    project: str
    text: str
    persona: str
    speed: float
    fields: dict
