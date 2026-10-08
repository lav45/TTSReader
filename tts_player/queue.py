"""Очередь сообщений: порядок по recv_ns, выдача предложений и хвост проигранных."""
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

    Последнее выданное next_item() (предложение или сообщение со склейкой) считается
    играющим до следующего вызова next_item(): тот означает, что оно закончено (проиграно или пропущено).
    Склейка, привязанная attach() к сообщению, чьё предложение играет, делает играющим
    само сообщение: плеер переходит на склейку, и дальше оно живёт как выданное Message.
    """
    TAIL = 5
    DURATION_TOLERANCE_S = 0.05

    def __init__(self) -> None:
        self._forget()

    def _forget(self) -> None:
        self._messages: list[Message] = []
        self._handed: set[Sentence] = set()
        self._started: list[Message] = []
        self._current: Message | None = None
        self._playing: Sentence | Message | None = None
        self._audio_played: set[Message] = set()  # играли склейкой: выданы Message или переведены attach()
        self._evicted: set[tuple[str, str]] = set()
        self._durations: dict[Path, float] = {}

    @property
    def current(self) -> Message | None:
        return self._current

    @property
    def playing(self) -> Sentence | Message | None:
        """Выданное next_item() и не законченное; после перехода attach() — само сообщение."""
        return self._playing

    def add(self, sentence: Sentence) -> None:
        if (sentence.session_id, sentence.token) in self._evicted:
            log.info("предложение вытесненного сообщения %s/%s не играется: %s",
                     sentence.session_id, sentence.token, sentence.path)
            self._unlink(sentence)
            return
        message = self._find(sentence.session_id, sentence.token)
        if message is None:
            message = Message(sentence.session_id, sentence.token, [])
            self._messages.append(message)
        keys = [s.recv_ns for s in message.sentences]
        message.sentences.insert(bisect.bisect_right(keys, sentence.recv_ns), sentence)
        self._messages.sort(key=lambda m: m.sentences[0].recv_ns)

    def next_item(self) -> Sentence | Message | None:
        """Следующее к проигрыванию: Sentence, Message со склейкой или None.

        Sentence играется как есть. Message выдаётся, когда стартует сообщение
        с audio: Player играет message.audio с нуля и по окончании снова зовёт
        next_item(). None — играть нечего, ждать. Сообщение с audio предложениями
        не выдаётся никогда; сообщение, чья склейка не сошлась по длительности
        (audio None), выдаётся по предложениям, даже если attach() его обработал.
        """
        self._playing = self._pick()
        if isinstance(self._playing, Sentence):
            self._handed.add(self._playing)
        elif self._playing is not None:
            self._audio_played.add(self._playing)
        return self._playing

    def release(self) -> list[Sentence]:
        """Отдаёт незаконченные предложения по recv_ns и забывает все сообщения.

        Незаконченные — играющее и те, что ещё были бы выданы: невыданные текущего
        и не начатых сообщений. Файлы остальных (законченные, опоздавшие к уже
        законченным сообщениям) удаляются. Прерванное сообщение со склейкой (играющее
        или текущее) отдаётся всеми предложениями: копия audio удаляется, после
        перезапуска оно играется по предложениям. Склейка, после выдачи которой (или
        перехода на неё через attach()) next_item() вызывали снова, доиграна: её файлы
        удаляются, предложения не возвращаются.
        """
        pending = self._interrupted()
        for message in self._messages:
            if message is not self._current and message in self._started:
                continue
            if message is self._current and self._finished_audio(message):
                continue
            if message.audio is not None and message not in self._started:
                pending.extend(s for s in message.sentences if s not in pending)
                continue
            pending.extend(s for s in message.sentences
                           if s not in self._handed and s not in pending)
        for message in self._messages:
            for sentence in message.sentences:
                if sentence not in pending:
                    self._unlink(sentence)
        for message in self._messages:
            self._unlink_audio(message)
        self._forget()
        return sorted(pending, key=lambda s: s.recv_ns)

    def _finished_audio(self, message: Message) -> bool:
        """Сообщение играло склейкой и уже не играет: next_item() после неё звали снова."""
        return message in self._audio_played and message is not self._playing

    def _interrupted(self) -> list[Sentence]:
        if isinstance(self._playing, Message):
            return list(self._playing.sentences)
        return [] if self._playing is None else [self._playing]

    def _pick(self) -> Sentence | Message | None:
        if self._current is not None:
            sentence = self._unplayed(self._current)
            if sentence is not None:
                return sentence
        for message in self._messages:
            if message in self._started:
                continue
            self._start(message)
            if message.audio is not None:
                return message
            sentence = self._unplayed(message)
            if sentence is not None:
                return sentence
        return None

    def _find(self, session_id: str, token: str) -> Message | None:
        for message in self._messages:
            if message.session_id == session_id and message.token == token:
                return message
        return None

    def _unplayed(self, message: Message) -> Sentence | None:
        if message.audio is not None:
            return None
        for sentence in message.sentences:
            if sentence not in self._handed:
                return sentence
        return None

    def _start(self, message: Message) -> None:
        self._current = message
        self._started.append(message)
        while len(self._started) > self.TAIL:
            self._drop(self._started.pop(0))

    def _drop(self, message: Message) -> None:
        self._messages.remove(message)
        self._audio_played.discard(message)
        self._evicted.add((message.session_id, message.token))
        for sentence in message.sentences:
            self._handed.discard(sentence)
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
        в любом случае. С audio сообщение next_item() предложениями больше не выдаёт;
        если играло его предложение, играющим становится само сообщение (переход на
        склейку): следующий next_item() заканчивает его целиком, release() до этого
        возвращает все его предложения.
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
        if self._playing in message.sentences:
            self._playing = message
            self._audio_played.add(message)
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
