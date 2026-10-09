"""Очередь сообщений: порядок прихода, связь с задачами демона, навигация, длина и хвост начатых."""
import bisect
import itertools
import logging
import shutil
import wave
from pathlib import Path
from typing import TYPE_CHECKING

from tts_player.model import Message, Sentence

if TYPE_CHECKING:
    from tts_player.history import HistoryEntry

log = logging.getLogger("tts_player.queue")


class MessageQueue:
    """Сообщения в порядке прихода задач хука; хвост из tail начатых сообщений.

    Очередь не знает, что играет: текущее сообщение и позицию держит Player. Очередь
    связывает предложения с сообщениями по id задач демона (link), отвечает на вопросы о
    порядке (following/preceding/first_unheard), длине и завершённости сообщения, отображает
    позицию в файл (locate) и владеет файлами своих сообщений: удаляет их при вытеснении из
    хвоста, в forget_audio() и clear().

    following/preceding/is_complete принимают только живые сообщения — из messages;
    сообщение, вытесненное или забытое после clear(), им не передаётся.

    tail задаёт снаружи Player (настройки плеера) и может меняться на ходу: start()
    подгоняет хвост к нему постепенно (см. start).
    """
    DURATION_TOLERANCE_S = 0.05

    def __init__(self, tail: int) -> None:
        self.tail = tail
        # Порядок постановки задач демону за всё время очереди (и забытых сообщений): демон
        # синтезирует задачи по одной в этом порядке.
        self._sequence = itertools.count(1)
        self._task_order: dict[str, int] = {}
        self._delivered = 0  # наибольший порядок задачи, от которой пришло предложение
        self._forget()

    def _forget(self) -> None:
        self._messages: list[Message] = []
        self._started: list[Message] = []
        self._durations: dict[Path, float] = {}

    @property
    def messages(self) -> list[Message]:
        """Сообщения в порядке прихода (копия списка)."""
        return list(self._messages)

    def append(self, message: Message) -> None:
        """Новое сообщение задачи хука — в конец; предложений у него ещё нет."""
        self._messages.append(message)

    def link(self, message: Message, task_id: str) -> None:
        """Задача демона task_id поставлена для message: её предложения — его."""
        message.task_ids.add(task_id)
        self._task_order[task_id] = next(self._sequence)

    def task_of(self, message: Message) -> str | None:
        """Последняя задача демона сообщения; None, если ни одна не поставлена."""
        return max(message.task_ids, key=self._task_order.__getitem__, default=None)

    def add(self, sentence: Sentence) -> None:
        """Добавляет предложение в сообщение его задачи. Задача чужая (нет сообщения:
        unknown, прошлый запуск, вытесненное или очищенное) — файл удалён, WARNING; сообщение
        прослушано — файл удалён."""
        message = self._find(sentence.task_id)
        if message is None:
            log.warning("чужое предложение %s (задача %s), удаляю", sentence.path.name,
                        sentence.task_id)
            self._unlink(sentence)
            return
        if message.heard:
            log.info("предложение прослушанного сообщения %s не играется: %s",
                     sentence.task_id, sentence.path)
            self._unlink(sentence)
            return
        self._delivered = max(self._delivered, self._task_order[sentence.task_id])
        received = [s.recv_ns for s in message.sentences]
        message.sentences.insert(bisect.bisect_right(received, sentence.recv_ns), sentence)

    def forget_audio(self, message: Message) -> None:
        """Кэш сообщения устарел: файлы предложений и склейки удалены, сообщение — как только
        пришедшее (без предложений, записи истории и отметки «прослушано»)."""
        for sentence in message.sentences:
            self._unlink(sentence)
        self._unlink_audio(message)
        message.sentences = []
        message.audio = None
        message.created_at = None
        message.duration_s = None
        message.heard = False

    def following(self, message: Message) -> Message | None:
        index = self._messages.index(message) + 1
        return self._messages[index] if index < len(self._messages) else None

    def preceding(self, message: Message) -> Message | None:
        index = self._messages.index(message)
        return self._messages[index - 1] if index > 0 else None

    def first_unheard(self) -> Message | None:
        return next((m for m in self._messages if not m.heard), None)

    def start(self, message: Message) -> None:
        """Сообщение начато: входит в хвост; лишние начатые удаляются, самые старые первыми.

        Хвост больше tail (tail уменьшили) сокращается постепенно: за один start начатых
        становится не больше чем на одно меньше, чем было до него. Пока хвост не больше
        tail, новое начатое вытесняет самое старое только сверх tail.
        """
        if message not in self._messages:
            log.debug("сообщение %s/%s уже не в очереди, не начинается",
                      message.session_id, self.task_of(message))
            return
        if message in self._started:
            return
        limit = max(self.tail, len(self._started) - 1)
        self._started.append(message)
        while len(self._started) > limit:
            self._drop(self._started.pop(0))

    def is_started(self, message: Message) -> bool:
        """Сообщение начато и ещё в хвосте (не вытеснено, не забыто)."""
        return message in self._started

    def mark_heard(self, message: Message) -> None:
        message.heard = True

    def length(self, message: Message) -> float:
        """Длина сообщения, с: склейки, если она есть, иначе сумма полученных предложений."""
        if message.audio is not None:
            return message.duration_s
        return sum(self._duration(s) for s in message.sentences)

    def is_complete(self, message: Message) -> bool:
        """Сообщение больше не растёт: есть склейка или запись истории; его задача не ушла в
        демон (ждать нечего); или пришло предложение задачи, поставленной позже его последней,
        — демон синтезирует задачи строго по одной в порядке постановки.
        """
        if message.audio is not None or message.created_at is not None or not message.sent:
            return True
        task_id = self.task_of(message)
        return task_id is not None and self._delivered > self._task_order[task_id]

    def clear(self) -> None:
        """Удаляет файлы всех сообщений, включая хвост, и забывает их вместе с задачами:
        поздние предложения очищенных — чужие (add удаляет их файлы). Новые сообщения
        приходят как обычно."""
        for message in self._messages:
            for sentence in message.sentences:
                self._unlink(sentence)
            self._unlink_audio(message)
        self._forget()

    def _find(self, task_id: str) -> Message | None:
        return next((m for m in self._messages if task_id in m.task_ids), None)

    def _drop(self, message: Message) -> None:
        self._messages.remove(message)
        for sentence in message.sentences:
            self._unlink(sentence)
        self._unlink_audio(message)
        log.info("сообщение %s/%s удалено из хвоста", message.session_id, self.task_of(message))

    def _unlink(self, sentence: Sentence) -> None:
        self._durations.pop(sentence.path, None)
        self._unlink_path(sentence.path)

    def _unlink_audio(self, message: Message) -> None:
        if message.audio is not None:
            self._unlink_path(message.audio)

    @staticmethod
    def _unlink_path(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            log.debug("файл уже удалён: %s", path)
        except OSError:
            log.exception("не удалось удалить файл %s", path)

    def attach(self, entry: "HistoryEntry", store: Path) -> Message | None:
        """Привязывает склейку демона к сообщению её сессии; None, если сообщения нет.

        Сообщение — с наибольшим первым recv_ns среди сообщений сессии с предложениями,
        без audio и записи истории, все предложения которых получены раньше entry.created_at. При совпадении
        длительности (допуск DURATION_TOLERANCE_S) склейка копируется в store/ и
        становится audio; иначе (или при сбое копирования) audio остаётся None, а
        сообщение доигрывается по предложениям. created_at/project заполняются
        в любом случае: сообщение считается завершённым (is_complete). Переход
        играющего сообщения на склейку решает Player.
        """
        message = self._candidate(entry)
        if message is None:
            log.info("для склейки %s нет сообщения сессии %s", entry.path.name,
                     entry.session_id)
            return None
        message.created_at = entry.created_at
        message.project = entry.project
        total = sum(self._duration(s) for s in message.sentences)
        if abs(entry.duration_s - total) > self.DURATION_TOLERANCE_S + 1e-9:
            log.warning("склейка %s: %.3f с, предложения сообщения %s/%s: %.3f с;"
                        " играется по предложениям", entry.path.name, entry.duration_s,
                        message.session_id, self.task_of(message), total)
            return message
        target = store / entry.path.name
        try:
            shutil.copy2(entry.path, target)
        except OSError:
            log.warning("не удалось скопировать склейку %s в %s; играется по"
                        " предложениям", entry.path, target, exc_info=True)
            target.unlink(missing_ok=True)
            return message
        message.audio = target
        message.duration_s = entry.duration_s
        return message

    def locate(self, message: Message, position_s: float) -> tuple[Path, float]:
        """Файл и смещение в нём для позиции в сообщении (за концом — конец последнего).

        Предложение, которое кончается после позиции или начинается на ней: предложение
        нулевой длины (битый файл) на позиции не пропускается. Сообщение без звука
        (ни предложений, ни склейки) сюда не передаётся.
        """
        if message.audio is not None:
            return message.audio, position_s
        remaining = max(position_s, 0.0)
        for sentence in message.sentences:
            duration = self._duration(sentence)
            if remaining < duration or remaining <= 0:
                return sentence.path, remaining
            remaining -= duration
        last = message.sentences[-1]
        return last.path, self._duration(last)

    def offset_of(self, message: Message, sentence: Sentence) -> float:
        """Начало предложения в сообщении, с."""
        offset = 0.0
        for item in message.sentences:
            if item == sentence:
                break
            offset += self._duration(item)
        return offset

    def _candidate(self, entry: "HistoryEntry") -> Message | None:
        created_ns = entry.created_at * 1e9
        fitting = [m for m in self._messages
                   if m.session_id == entry.session_id and m.sentences and m.audio is None
                   and m.created_at is None
                   and all(s.recv_ns < created_ns for s in m.sentences)]
        return max(fitting, key=lambda m: m.sentences[0].recv_ns, default=None)

    def _duration(self, sentence: Sentence) -> float:
        cached = self._durations.get(sentence.path)
        if cached is not None:
            return cached
        try:
            with wave.open(str(sentence.path), "rb") as w:
                duration = w.getnframes() / w.getframerate()
        except (OSError, EOFError, wave.Error, ZeroDivisionError):
            log.warning("длительность %s не прочитана, считается 0", sentence.path,
                        exc_info=True)
            duration = 0.0
        self._durations[sentence.path] = duration
        return duration
