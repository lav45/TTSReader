"""Очередь сообщений: порядок прихода, связь с задачами демона, навигация, длина, хвост начатых
и обрезка до tail."""
import bisect
import itertools
import logging
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from tts_player.model import UNKNOWN_TASK, Message, Sentence

log = logging.getLogger("tts_player.queue")


@dataclass
class QueueSnapshot:
    """Очередь между запусками плеера (MessageQueue.release → messages.json → restore).

    started — начатые (хвост) из messages, от старых к новым; task_order — порядок
    постановки задач демону для id задач этих сообщений; delivered — наибольший порядок
    задачи, от которой пришло предложение: по ним restore восстанавливает завершённость."""
    messages: list[Message]
    started: list[Message]
    task_order: dict[str, int]
    delivered: int


class TaskOrder:
    """Порядок постановки задач демону для всех сессий и наибольший доставленный.

    Демон синтезирует задачи всех сессий строго по одной в порядке постановки, поэтому
    порядок один на все очереди сессий (MessageQueue): предложение поздней задачи одной
    сессии завершает более раннее сообщение другой. Помнит задачи за всё время, в том
    числе забытых сообщений."""

    def __init__(self) -> None:
        self._sequence = itertools.count(1)
        self._order: dict[str, int] = {}
        self._delivered = 0  # наибольший порядок задачи, от которой пришло предложение

    def link(self, task_id: str) -> None:
        """Задача поставлена демону: ей следующий номер."""
        self._order[task_id] = next(self._sequence)

    def order(self, task_id: str) -> int:
        return self._order[task_id]

    def deliver(self, task_id: str) -> None:
        """От задачи пришло предложение."""
        self._delivered = max(self._delivered, self._order[task_id])

    def delivered_after(self, task_id: str) -> bool:
        """Доставлена задача, поставленная позже task_id."""
        return self._delivered > self._order[task_id]

    def snapshot(self, task_ids: Iterable[str]) -> tuple[dict[str, int], int]:
        """(порядок указанных задач, наибольший доставленный) — для QueueSnapshot."""
        return {task_id: self._order[task_id] for task_id in task_ids}, self._delivered

    def restore(self, task_order: dict[str, int], delivered: int) -> None:
        """Принимает порядок прошлого запуска; новые задачи — после всех известных и
        доставленных (clear забывает задачи, но не delivered): иначе новое сообщение
        считалось бы завершённым с первого предложения."""
        self._order.update(task_order)
        self._delivered = max(self._delivered, delivered)
        last = max([*self._order.values(), self._delivered])
        self._sequence = itertools.count(last + 1)


class MessageQueue:
    """Сообщения в порядке прихода задач хука; хвост из tail начатых сообщений.

    Очередь не знает, что играет: текущее сообщение и позицию держит Player. Очередь
    связывает предложения с сообщениями по id задач демона (link), отвечает на вопросы о
    порядке (following), длине и завершённости сообщения, отображает
    позицию в файл (locate) и владеет файлами своих сообщений: удаляет их при вытеснении из
    хвоста, обрезке (trim), в forget_audio() и clear(); на выходе плеера отдаёт их вместе с
    сообщениями снимком (release), на старте принимает снимок прошлого запуска (restore).

    following/is_complete принимают только живые сообщения — из messages;
    сообщение, вытесненное, обрезанное или забытое после clear()/release(), им не передаётся.

    tail задаёт снаружи Player (настройки плеера) и может меняться на ходу: start()
    подгоняет хвост начатых к нему постепенно (см. start), trim() режет всю очередь сразу —
    когда её звать, решает Player (фокус).
    """

    def __init__(self, tail: int, order: TaskOrder) -> None:
        self.tail = tail
        self._order = order  # общий с очередями других сессий
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
        self._order.link(task_id)

    def task_of(self, message: Message) -> str | None:
        """Последняя задача демона сообщения; None, если ни одна не поставлена."""
        return max(message.task_ids, key=self._order.order, default=None)

    def task_of_pass(self, session_id: str, token: str) -> str | None:
        """id задачи прохода синтеза token сессии session_id по уже полученным предложениям или
        None: метка прохода случайна у каждого прохода демона, поэтому однозначна."""
        return next((s.task_id for m in self._messages for s in m.sentences
                     if s.token == token and s.session_id == session_id
                     and s.task_id != UNKNOWN_TASK), None)

    def owns(self, task_id: str) -> bool:
        """Задача — одного из живых сообщений очереди."""
        return self.message_of(task_id) is not None

    def add(self, sentence: Sentence, sounded: Sentence | None = None) -> None:
        """Добавляет предложение в сообщение его задачи. Задача чужая (нет сообщения:
        unknown, прошлый запуск, вытесненное или очищенное) — файл удалён, WARNING; сообщение
        прослушано — файл удалён.

        Дубль индекса той же задачи (пауза демона: новый проход начат с оборванного
        предложения) — остаётся предложение позднего прохода (больший recv_ns), файл другого
        удалён; но уже звучавшее (sounded — играющее предложение сообщения, и всё до него)
        не заменяется: удаляется пришедшее."""
        message = self.message_of(sentence.task_id)
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
        self._order.deliver(sentence.task_id)
        if not self._replace_duplicate(message, sentence, sounded):
            return
        received = [s.recv_ns for s in message.sentences]
        message.sentences.insert(bisect.bisect_right(received, sentence.recv_ns), sentence)

    def _replace_duplicate(self, message: Message, sentence: Sentence,
                           sounded: Sentence | None) -> bool:
        """Дубль индекса sentence в message: True — sentence добавлять (дубля нет или он убран),
        False — sentence удалено."""
        duplicate = next((s for s in message.sentences
                          if s.task_id == sentence.task_id and s.index == sentence.index), None)
        if duplicate is None:
            return True
        played = (message.sentences[:message.sentences.index(sounded) + 1]
                 if sounded in message.sentences else [])
        if duplicate in played or sentence.recv_ns < duplicate.recv_ns:
            log.info("дубль предложения %d задачи %s, оставлено %s", sentence.index,
                     sentence.task_id, duplicate.path.name)
            self._unlink(sentence)
            return False
        log.info("дубль предложения %d задачи %s, оставлено %s", sentence.index,
                 sentence.task_id, sentence.path.name)
        message.sentences.remove(duplicate)
        self._unlink(duplicate)
        return True

    def forget_audio(self, message: Message) -> None:
        """Кэш сообщения устарел: файлы предложений удалены, сообщение — как только пришедшее
        (без предложений, отметки «прослушано» и доигранных индексов; число предложений
        остаётся — текст тот же)."""
        for sentence in message.sentences:
            self._unlink(sentence)
        message.sentences = []
        message.heard = False
        message.played_indices = set()

    def following(self, message: Message) -> Message | None:
        index = self._messages.index(message) + 1
        return self._messages[index] if index < len(self._messages) else None

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
            self._drop(self._started[0])

    def trim(self, keep: Message | None = None) -> None:
        """Оставляет tail последних сообщений: старшие — любые, начатые и нет, прослушанные и
        отложенные — удаляются с файлами, как при вытеснении из хвоста. keep (текущее) места
        в tail не занимает: старше них — остаётся сверх tail."""
        older = self._messages[:max(len(self._messages) - self.tail, 0)]
        for message in older:
            if message is not keep:
                self._drop(message)

    def is_started(self, message: Message) -> bool:
        """Сообщение начато и ещё в хвосте (не вытеснено, не забыто)."""
        return message in self._started

    def mark_heard(self, message: Message) -> None:
        message.heard = True

    def length(self, message: Message) -> float:
        """Длина сообщения, с: сумма полученных предложений."""
        return sum(self._duration(s) for s in message.sentences)

    def is_complete(self, message: Message) -> bool:
        """Сообщение больше не растёт: у его последней задачи пришли предложения со всеми
        индексами 0…n−1, где n — message.sentence_count (любого прохода; n == 0 — ждать
        нечего). Запасные признаки — число неизвестно или демон сбился: его задача не ушла в
        демон (ждать нечего); или пришло предложение задачи, поставленной позже его последней,
        — демон синтезирует задачи строго по одной в порядке постановки.
        """
        if not message.sent:
            return True
        task_id = self.task_of(message)
        if self._all_indices(message, task_id):
            return True
        return task_id is not None and self._order.delivered_after(task_id)

    @staticmethod
    def _all_indices(message: Message, task_id: str | None) -> bool:
        """У задачи task_id сообщения есть предложения со всеми индексами 0…sentence_count−1,
        вместе с удалёнными доигранными при выходе (played_indices); число неизвестно — False."""
        if message.sentence_count is None:
            return False
        received = {s.index for s in message.sentences if s.task_id == task_id}
        return (received | message.played_indices).issuperset(range(message.sentence_count))

    def clear(self) -> None:
        """Удаляет файлы всех сообщений, включая хвост, и забывает их вместе с задачами:
        поздние предложения очищенных — чужие (add удаляет их файлы). Новые сообщения
        приходят как обычно."""
        for message in self._messages:
            for sentence in message.sentences:
                self._unlink(sentence)
        self._forget()

    def release(self, current: Message | None = None, position_s: float = 0.0) -> QueueSnapshot:
        """Выход плеера: файлы прослушанных удалены (их текст остаётся), файлы остальных
        остаются в store/; очередь забывает всё и отдаёт снимок для следующего запуска.

        current (непрослушанное) на позиции position_s > 0: предложения, кончившиеся до
        неё, удаляются — следующий запуск продолжит с недоигранного (предложение
        нулевой длины на позиции и начинающееся на ней остаются); индексы удалённых
        предложений последней задачи остаются в played_indices для is_complete."""
        if current is not None and not current.heard and position_s > 0:
            self._drop_played(current, position_s)
        for message in self._messages:
            if message.heard:
                for sentence in message.sentences:
                    self._unlink(sentence)
                message.sentences = []
        task_ids = [task_id for message in self._messages for task_id in message.task_ids]
        task_order, delivered = self._order.snapshot(task_ids)
        snapshot = QueueSnapshot(self._messages, self._started, task_order, delivered)
        self._forget()
        return snapshot

    def _drop_played(self, message: Message, position_s: float) -> None:
        played = [s for s in message.sentences if not self._unfinished(message, s, position_s)]
        task_id = self.task_of(message)
        message.played_indices |= {s.index for s in played if s.task_id == task_id}
        for sentence in played:
            self._unlink(sentence)
        message.sentences = [s for s in message.sentences if s not in played]

    def _unfinished(self, message: Message, sentence: Sentence, position_s: float) -> bool:
        start = self.offset_of(message, sentence)
        return start + self._duration(sentence) > position_s or start >= position_s

    def restore(self, snapshot: QueueSnapshot) -> None:
        """Старт плеера: сообщения прошлого запуска — перед уже пришедшими, с их задачами
        (поздние предложения этих задач — их) и завершённостью; хвост начатых подгоняется
        к tail, лишние удаляются, самые старые первыми."""
        self._order.restore(snapshot.task_order, snapshot.delivered)
        self._messages[:0] = snapshot.messages
        self._started[:0] = snapshot.started
        while len(self._started) > self.tail:
            self._drop(self._started[0])

    def message_of(self, task_id: str) -> Message | None:
        return next((m for m in self._messages if task_id in m.task_ids), None)

    def _drop(self, message: Message) -> None:
        """Сообщение уходит из очереди и хвоста начатых, его файлы удалены."""
        self._messages.remove(message)
        if message in self._started:
            self._started.remove(message)
        for sentence in message.sentences:
            self._unlink(sentence)
        log.info("сообщение %s/%s удалено из очереди", message.session_id,
                 self.task_of(message))

    def _unlink(self, sentence: Sentence) -> None:
        self._durations.pop(sentence.path, None)
        try:
            sentence.path.unlink()
        except FileNotFoundError:
            log.debug("файл уже удалён: %s", sentence.path)
        except OSError:
            log.exception("не удалось удалить файл %s", sentence.path)

    def locate(self, message: Message, position_s: float) -> tuple[Path, float]:
        """Файл и смещение в нём для позиции в сообщении (за концом — конец последнего).

        Предложение, которое кончается после позиции или начинается на ней: предложение
        нулевой длины (битый файл) на позиции не пропускается. Сообщение без предложений
        сюда не передаётся.
        """
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
