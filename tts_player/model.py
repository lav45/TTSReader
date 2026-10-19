"""Модель данных плеера."""
from dataclasses import dataclass, field
from pathlib import Path

UNKNOWN_TASK = "unknown"
"""id задачи, когда демон не назвал приёмнику текущую задачу (контракт (13))."""


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
    индексы предложений, удалённых доигранными при выходе плеера (MessageQueue.release):
    последней задачи — у текста разом, индексы текста — у текста по предложениям; они пришли, и
    конец сообщения по числу их учитывает.
    starts — позиции начал предложений в text (счётчик, sentence_count штук); None — не
    посчитано. Известны — текст уходит демону по одному предложению (Player._feed): pieces —
    какое предложение текста (индекс) какой задачей демона отдано; в задаче оно одно, и его
    файл приходит с индексом 0. По pieces плеер знает, что уже озвучено, а что нет: вернувшись к
    дорожке, он отдаёт демону первое неотданное предложение. Задачи сообщения не из pieces —
    весь текст разом (счётчика нет, или сообщение озвучивалось до ответа счётчика). skipped —
    индексы, чья задача не записалась в очередь демона: в этом запуске не повторяются. fields —
    поля задачи хука как есть (в messages.json не сохраняются): с ними уходят предложения
    нового ответа, как раньше уходила вся задача; None — голос берётся из config.json.
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
    starts: list[int] | None = None
    pieces: dict[str, int] = field(default_factory=dict)
    skipped: set[int] = field(default_factory=set)
    fields: dict | None = None


@dataclass(frozen=True)
class HookTask:
    """Задача хука claude-tts из очереди хука hook-queue/ (контракт (11)).

    fields — вся задача как есть: из неё берут остальное (голос, метод скорости и т.д.)."""
    id: str
    timestamp: float
    session_id: str
    project: str
    text: str
    persona: str
    speed: float
    fields: dict
