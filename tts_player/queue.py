"""Очередь сообщений: порядок по recv_ns, навигация, длина сообщений и хвост начатых."""
import bisect
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
    """Сообщения по первому recv_ns; хвост из TAIL начатых сообщений.

    Очередь не знает, что играет: текущее сообщение и позицию держит Player. Очередь
    отвечает на вопросы о порядке (following/preceding/first_unheard), длине и
    завершённости сообщения, отображает позицию в файл (locate) и владеет файлами
    своих сообщений: удаляет их при вытеснении из хвоста, в release() и clear().
    """
    TAIL = 5
    DURATION_TOLERANCE_S = 0.05

    def __init__(self) -> None:
        self._forget()

    def _forget(self) -> None:
        self._messages: list[Message] = []
        self._started: list[Message] = []
        self._evicted: set[tuple[str, str]] = set()
        self._durations: dict[Path, float] = {}

    @property
    def messages(self) -> list[Message]:
        """Сообщения по recv_ns первого предложения (копия списка)."""
        return list(self._messages)

    def add(self, sentence: Sentence) -> None:
        """Добавляет предложение в его сообщение; к прослушанному или вытесненному — удаляет файл."""
        message = self._find(sentence.session_id, sentence.token)
        late = (sentence.session_id, sentence.token) in self._evicted or (
            message is not None and message.heard)
        if late:
            log.info("предложение законченного сообщения %s/%s не играется: %s",
                     sentence.session_id, sentence.token, sentence.path)
            self._unlink(sentence)
            return
        if message is None:
            message = Message(sentence.session_id, sentence.token, [])
            self._messages.append(message)
        keys = [s.recv_ns for s in message.sentences]
        message.sentences.insert(bisect.bisect_right(keys, sentence.recv_ns), sentence)
        self._messages.sort(key=lambda m: m.sentences[0].recv_ns)

    def following(self, message: Message) -> Message | None:
        index = self._messages.index(message) + 1
        return self._messages[index] if index < len(self._messages) else None

    def preceding(self, message: Message) -> Message | None:
        index = self._messages.index(message)
        return self._messages[index - 1] if index > 0 else None

    def first_unheard(self) -> Message | None:
        return next((m for m in self._messages if not m.heard), None)

    def start(self, message: Message) -> None:
        """Сообщение начато: входит в хвост; при TAIL+1 начатых удаляется самое старое."""
        if message in self._started:
            return
        self._started.append(message)
        while len(self._started) > self.TAIL:
            self._drop(self._started.pop(0))

    def mark_heard(self, message: Message) -> None:
        message.heard = True

    def length(self, message: Message) -> float:
        """Длина сообщения, с: склейки, если она есть, иначе сумма полученных предложений."""
        if message.audio is not None:
            return message.duration_s
        return sum(self._duration(s) for s in message.sentences)

    def is_complete(self, message: Message) -> bool:
        """Сообщение больше не растёт: есть склейка или запись истории, или есть более позднее.

        Более позднее сообщение означает новый token: демон синтезирует сообщения
        строго по одному.
        """
        return (message.audio is not None or message.created_at is not None
                or self.following(message) is not None)

    def release(self, current: Message | None, position_s: float) -> list[Sentence]:
        """Отдаёт непрослушанные предложения по recv_ns и забывает все сообщения.

        current — предложения, кончающиеся после position_s; остальные непрослушанные
        сообщения — все предложения. Файлы прослушанных и сыгранных предложений
        удаляются, копии склеек удаляются всегда: после перезапуска непрослушанное
        играется по предложениям.
        """
        pending: list[Sentence] = []
        for message in self._messages:
            if message is current:
                pending.extend(s for s in message.sentences
                               if self.offset_of(message, s) + self._duration(s) > position_s)
            elif not message.heard:
                pending.extend(message.sentences)
        for message in self._messages:
            for sentence in message.sentences:
                if sentence not in pending:
                    self._unlink(sentence)
            self._unlink_audio(message)
        self._forget()
        return sorted(pending, key=lambda s: s.recv_ns)

    def clear(self) -> None:
        """Удаляет файлы всех сообщений, включая хвост, и забывает всё."""
        for message in self._messages:
            for sentence in message.sentences:
                self._unlink(sentence)
            self._unlink_audio(message)
        self._forget()

    def _find(self, session_id: str, token: str) -> Message | None:
        for message in self._messages:
            if message.session_id == session_id and message.token == token:
                return message
        return None

    def _drop(self, message: Message) -> None:
        self._messages.remove(message)
        self._evicted.add((message.session_id, message.token))
        for sentence in message.sentences:
            self._unlink(sentence)
        self._unlink_audio(message)
        log.info("сообщение %s/%s удалено из хвоста", message.session_id, message.token)

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

        Сообщение — с наибольшим первым recv_ns среди сообщений сессии без audio,
        все предложения которых получены раньше entry.created_at. При совпадении
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
                        " играется по предложениям", entry.path.name,
                        entry.duration_s, message.session_id, message.token, total)
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
        """Файл и смещение в нём для позиции в сообщении (за концом — конец последнего)."""
        if message.audio is not None:
            return message.audio, position_s
        remaining = max(position_s, 0.0)
        for sentence in message.sentences:
            duration = self._duration(sentence)
            if remaining < duration:
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
                   if m.session_id == entry.session_id and m.audio is None
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
