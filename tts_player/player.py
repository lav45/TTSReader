"""Плеер-контроллер: курсор по очереди, команды кнопок, снимок состояния для окна."""
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from tts_player.history import History, HistoryEntry  # noqa: E402
from tts_player.inbox import Inbox  # noqa: E402
from tts_player.model import Message, Sentence  # noqa: E402
from tts_player.playback import Playback  # noqa: E402
from tts_player.queue import MessageQueue  # noqa: E402

log = logging.getLogger("tts_player.player")

Status = Literal["playing", "paused", "stopped", "idle"]


@dataclass(frozen=True)
class PlayerState:
    """Снимок для окна. idle — нечего играть: current доигран (или None, если сообщений нет)."""
    status: Status
    current: Message | None
    position_s: float  # внутри текущего сообщения
    length_s: float  # queue.length(current), 0 без текущего
    messages: list[Message]
    output_down: bool
    lengths: list[float]  # queue.length(m) для каждого из messages, в том же порядке
    completes: list[bool]  # queue.is_complete(m) для каждого из messages, в том же порядке


class Player:
    """Курсор (текущее сообщение + позиция) поверх очереди; команды кнопок из спеки.

    Играется файл queue.locate(current, позиция); доигравший файл сменяется следующим
    предложением сообщения. Сообщение кончилось и завершено (queue.is_complete) —
    прослушано, дальше следующее или первое непрослушанное; не завершено — статус
    playing без звука, пока не придёт предложение. На паузе и после stop автоперехода нет.

    Ошибка файла — пропуск. Отказ вывода звука — тот же файл с той же позиции через
    RETRY_SECONDS, строка error — одна на отказ, а не на каждый повтор. Отказ кончается,
    когда звук пошёл (Playback.on_started); повтор, упавший до звука, — тот же отказ.

    Склейка играющего по предложениям сообщения подменяет их с той же позиции. Пока
    вывод недоступен, звука нет — повтор начнёт склейку с позиции, с которой запускался
    несыгравший файл (начало предложения или точка seek/продолжения в нём).
    На паузе и после stop склейка лишь запоминается: следующий запуск найдёт её locate.
    """
    RETRY_SECONDS = 2

    def __init__(self, runtime_dir: Path, audio_sink: str | None,
                 history_dir: Path | None, history_db: Path | None) -> None:
        self.on_change: Callable[[PlayerState], None] = lambda state: None
        self._queue = MessageQueue()
        self._playback = Playback(audio_sink)
        self._playback.on_finished = self._on_finished
        self._playback.on_error = self._on_error
        self._playback.on_started = self._on_started
        self._inbox = Inbox(runtime_dir, self._on_sentence)
        self._history = None
        if history_dir is not None and history_db is not None:
            self._history = History(history_dir, history_db, self._on_entry)
        self._history_dir = history_dir
        self._status: Status = "idle"
        self._current: Message | None = None
        # Позиция, с которой запущен _file (без файла — курсор); _file_base — начало
        # _file в сообщении. _sentence — предложение _file или последнее доигравшее,
        # None при склейке.
        self._position = 0.0
        self._file: Path | None = None
        self._file_base = 0.0
        self._sentence: Sentence | None = None
        self._output_down = False
        self._retry_id = 0

    def start(self) -> None:
        """История — раньше inbox/: накопленное в inbox/ начинает играть уже в start(),
        и склейка, записанная после первой строки playing, не должна считаться
        существовавшей до запуска. Колбэки обеих частей идут из цикла GLib, поэтому
        такой порядок ничего не теряет."""
        if self._history is not None:
            try:
                self._history.start()
            except OSError as error:
                log.warning("история %s недоступна (%s), работаю без склеек",
                            self._history_dir, error)
        self._inbox.start()
        self._changed()

    def shutdown(self) -> None:
        """Выход процесса: незаконченное — обратно в inbox/, законченное удаляет очередь."""
        self._inbox.stop()
        if self._history is not None:
            self._history.stop()
        position = self._now()
        self._halt()
        self._inbox.put_back(self._queue.release(self._current, position))

    def state(self) -> PlayerState:
        current = self._current
        length = 0.0 if current is None else self._queue.length(current)
        messages = self._queue.messages
        lengths = [self._queue.length(m) for m in messages]
        completes = [self._queue.is_complete(m) for m in messages]
        return PlayerState(self._status, current, self._now(), length,
                           messages, self._output_down, lengths, completes)

    def toggle_pause(self) -> None:
        if self._status == "playing":
            self._position = self._now()
            self._halt()
            self._status = "paused"
        elif self._status == "paused":
            self._status = "playing"
            self._play_at(self._position)
        self._changed()

    def stop_playback(self) -> None:
        """Идемпотентный stop (команда `tts-player stop`): играет или на паузе — как stop
        кнопки play|stop; stopped и idle — без изменений."""
        if self._status in ("playing", "paused"):
            self._stop()
            self._changed()

    def play_if_stopped(self) -> None:
        """Команда `tts-player play`: stopped и idle с текущим сообщением — как play кнопки
        play|stop; playing, paused и отсутствие сообщения — без изменений."""
        if self._status in ("stopped", "idle") and self._current is not None:
            self.toggle_play_stop()

    def _stop(self) -> None:
        self._halt()
        self._status = "stopped"
        self._position = 0.0

    def toggle_play_stop(self) -> None:
        """stop — позиция на начало, автоперехода нет; play — текущее с позиции курсора
        (после stop это начало; после конца сообщения — снова с начала)."""
        if self._status in ("playing", "paused"):
            self._stop()
        elif self._current is not None:
            position = self._position
            if position >= self._queue.length(self._current):
                position = 0.0
            self._status = "playing"
            self._play_at(position)
        self._changed()

    def next(self) -> None:
        current = self._current
        following = None if current is None else self._queue.following(current)
        if following is not None:
            self._leave(current)
            self._begin(following)
        self._changed()

    def prev(self) -> None:
        current = self._current
        preceding = None if current is None else self._queue.preceding(current)
        if preceding is not None:
            self._leave(current)
            self._begin(preceding)
        self._changed()

    def play_message(self, message: Message) -> None:
        """Клик по строке списка: сообщение становится текущим и играет с начала, из любого
        статуса. Покинутое прослушано, только если завершено (как у next/prev); клик по
        текущему и по вытесненному из очереди сообщению ничего не меняет."""
        if not any(message is queued for queued in self._queue.messages):
            return
        current = self._current
        if message is current:
            return
        if current is not None:
            self._leave(current)
        self._begin(message)
        self._changed()

    def seek(self, position_s: float) -> None:
        """Позиция в текущем сообщении, в пределах [0, length]; статус не меняется."""
        current = self._current
        if current is not None:
            position = min(max(position_s, 0.0), self._queue.length(current))
            if self._status == "playing":
                self._halt()
                self._play_at(position)
            else:
                self._position = position
        self._changed()

    def clear(self) -> None:
        """Очистка буфера: звук стоп, все файлы удалены; новые предложения играют как обычно."""
        self._halt()
        self._queue.clear()
        self._current = None
        self._sentence = None
        self._position = 0.0
        self._status = "idle"
        self._output_down = False
        self._changed()

    def _on_sentence(self, sentence: Sentence) -> None:
        self._queue.add(sentence)
        if self._status == "idle":
            message = self._queue.first_unheard()
            if message is not None:
                self._begin(message)
        elif self._waiting():
            following = self._sentence_after(self._sentence)
            if following is not None:
                self._play_sentence(following)
            else:
                self._message_end()
        self._changed()

    def _on_entry(self, entry: HistoryEntry) -> None:
        message = self._queue.attach(entry, self._inbox.store)
        if message is None:
            return
        if message.audio is None:
            log.info("ended %s %s", message.session_id, message.token)
        else:
            log.info("complete %s %s %.2fs", message.session_id, message.token,
                     message.duration_s)
        if message is self._current and self._status == "playing":
            if self._file is None:
                self._message_end()
            elif message.audio is not None:
                self._switch(message)
        self._changed()

    def _on_started(self, path: Path) -> None:
        if self._output_down:
            self._output_down = False
            self._changed()

    def _on_finished(self, path: Path) -> None:
        self._output_down = False
        self._after_file()
        self._changed()

    def _on_error(self, path: Path, text: str, output_failed: bool) -> None:
        if not output_failed:
            self._output_down = False
            log.info("error %s: %s", path.name, text)
            self._after_file()
            self._changed()
            return
        if not self._output_down:
            log.info("error %s: %s", path.name, text)
        self._output_down = True
        self._retry_id = GLib.timeout_add_seconds(self.RETRY_SECONDS, self._retry)
        self._changed()

    def _retry(self) -> bool:
        self._retry_id = 0
        # Повтор ставится только из on_error играющего файла; всё, что сбрасывает _file,
        # проходит через _halt и снимает повтор.
        assert self._file is not None, "повтор без играющего файла"
        self._playback.play(self._file, start_s=self._position - self._file_base)
        return GLib.SOURCE_REMOVE

    def _switch(self, message: Message) -> None:
        position = self._now()
        log.info("switched %s at %.2f", message.audio.name, position)
        self._sentence = None
        self._file = message.audio
        self._file_base = 0.0
        self._position = position
        if not self._output_down:
            self._playback.play(message.audio, start_s=position)

    def _after_file(self) -> None:
        """Файл доиграл или пропущен: следующее предложение или конец сообщения."""
        self._file = None
        following = None if self._sentence is None else self._sentence_after(self._sentence)
        if following is not None:
            self._play_sentence(following)
            return
        self._position = self._queue.length(self._current)
        self._message_end()

    def _message_end(self) -> None:
        current = self._current
        if not self._queue.is_complete(current):
            return  # ждём предложение: статус playing, звук молчит
        self._queue.mark_heard(current)
        upcoming = self._queue.following(current) or self._queue.first_unheard()
        if upcoming is None:
            self._status = "idle"
            return
        self._begin(upcoming)

    def _leave(self, message: Message) -> None:
        """Сообщение покинуто по next/prev: звук стоп; прослушанным считается только
        завершённое — у растущего ещё придут предложения."""
        self._halt()
        if self._queue.is_complete(message):
            self._queue.mark_heard(message)

    def _begin(self, message: Message) -> None:
        self._queue.start(message)
        self._current = message
        self._status = "playing"
        self._play_at(0.0)

    def _play_at(self, position: float) -> None:
        current = self._current
        path, offset = self._queue.locate(current, position)
        self._sentence = None
        if current.audio is None:
            self._sentence = next(s for s in current.sentences if s.path == path)
        self._start_file(path, position - offset, position)

    def _play_sentence(self, sentence: Sentence) -> None:
        self._sentence = sentence
        base = self._queue.offset_of(self._current, sentence)
        self._start_file(sentence.path, base, base)

    def _start_file(self, path: Path, base: float, position: float) -> None:
        self._file = path
        self._file_base = base
        self._position = position
        log.info("playing %s", path.name)
        self._playback.play(path, start_s=position - base)

    def _halt(self) -> None:
        self._playback.stop()
        if self._retry_id:
            GLib.source_remove(self._retry_id)
            self._retry_id = 0
        self._file = None

    def _waiting(self) -> bool:
        """Сообщение доиграно до конца полученного и не завершено: ждём предложение."""
        return self._status == "playing" and self._file is None

    def _sentence_after(self, sentence: Sentence) -> Sentence | None:
        sentences = self._current.sentences
        index = sentences.index(sentence) + 1
        return sentences[index] if index < len(sentences) else None

    def _now(self) -> float:
        """Позиция в текущем сообщении: начало играющего файла плюс позиция в нём."""
        if self._file is None:
            return self._position
        at = self._playback.position()
        return self._position if at is None else self._file_base + at

    def _changed(self) -> None:
        try:
            self.on_change(self.state())
        except Exception:
            log.exception("on_change: ошибка обработчика окна")
