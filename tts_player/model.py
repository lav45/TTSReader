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
    project — из задачи хука.
    heard — сообщение прослушано (MessageQueue.mark_heard): его поздние предложения
    не добавляются.
    sentence_count — на сколько предложений демон делит text (счётчик SentenceCounter той же
    функцией claude-tts); None — ещё не посчитано или счётчик недоступен. played_indices —
    индексы предложений последней задачи, удалённых доигранными при выходе плеера
    (MessageQueue.release): они пришли, и конец сообщения по числу их учитывает.
    rest_at — позиция второго предложения в text (счётчик); None — предложений меньше двух или
    не посчитано. head_task — задача демона только с первым предложением отложенного сообщения,
    отданная заранее, пока доигрывает предыдущее (Player._prefetch): пока сообщение отложено,
    остальные предложения ждут начала проигрывания и уходят своей задачей; None — первое
    предложение отдельно не отдавалось или озвучка начата заново.
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
    heard: bool = False
    deferred: bool = False
    sentence_count: int | None = None
    played_indices: set[int] = field(default_factory=set)
    rest_at: int | None = None
    head_task: str | None = None


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
