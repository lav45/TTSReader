"""Очередь сообщений: порядок по recv_ns, выдача предложений и хвост проигранных."""
import bisect
import logging

from tts_player.model import Message, Sentence

log = logging.getLogger("tts_player.queue")


class MessageQueue:
    TAIL = 5

    def __init__(self) -> None:
        self._messages: list[Message] = []
        self._handed: set[Sentence] = set()
        self._started: list[Message] = []
        self._current: Message | None = None

    @property
    def current(self) -> Message | None:
        return self._current

    def add(self, sentence: Sentence) -> None:
        message = self._find(sentence.session_id, sentence.token)
        if message is None:
            message = Message(sentence.session_id, sentence.token, [])
            self._messages.append(message)
        if sentence in message.sentences:
            return
        keys = [s.recv_ns for s in message.sentences]
        message.sentences.insert(bisect.bisect_right(keys, sentence.recv_ns), sentence)
        self._messages.sort(key=lambda m: m.sentences[0].recv_ns)

    def next_sentence(self) -> Sentence | None:
        if self._current is not None:
            sentence = self._unplayed(self._current)
            if sentence is not None:
                self._handed.add(sentence)
                return sentence
        for message in self._messages:
            if message in self._started:
                continue
            self._start(message)
            sentence = self._unplayed(message)
            self._handed.add(sentence)
            return sentence
        return None

    def _find(self, session_id: str, token: str) -> Message | None:
        for message in self._messages:
            if message.session_id == session_id and message.token == token:
                return message
        return None

    def _unplayed(self, message: Message) -> Sentence | None:
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
        for sentence in message.sentences:
            self._handed.discard(sentence)
            try:
                sentence.path.unlink()
            except FileNotFoundError:
                log.debug("файл уже удалён: %s", sentence.path)
        self._messages.remove(message)
        log.info("сообщение %s/%s удалено из хвоста", message.session_id, message.token)
