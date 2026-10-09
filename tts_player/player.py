"""Плеер-контроллер: курсор по очереди, команды кнопок, снимок состояния для окна."""
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from tts_player.control import ControlRequests  # noqa: E402
from tts_player.daemon_queue import DaemonQueue  # noqa: E402
from tts_player.daemon_settings import ActiveVoice, DaemonSettings  # noqa: E402
from tts_player.ducking import Ducker  # noqa: E402
from tts_player.history import History, HistoryEntry  # noqa: E402
from tts_player.inbox import Inbox  # noqa: E402
from tts_player.hook_tasks import HookTasks  # noqa: E402
from tts_player.model import HookTask, Message, Sentence  # noqa: E402
from tts_player.playback import Playback  # noqa: E402
from tts_player.player_settings import Prefs  # noqa: E402
from tts_player.queue import MessageQueue  # noqa: E402
from tts_player.saved_messages import SavedMessages  # noqa: E402
from tts_player.sessions import Sessions  # noqa: E402
from tts_player.settings_error import SettingsError  # noqa: E402

log = logging.getLogger("tts_player.player")

Status = Literal["playing", "paused", "stopped", "idle"]

LOGGED_TEXT_CHARS = 80  # столько знаков текста задачи в строке о сбое её записи


@dataclass(frozen=True)
class PlayerState:
    """Снимок для окна. idle — ничего не играет: current доигран либо ещё не начат
    (при выключенном автовоспроизведении), или None, если сообщений нет."""
    status: Status
    current: Message | None
    position_s: float  # внутри текущего сообщения
    length_s: float  # queue.length(current), 0 без текущего
    messages: list[Message]
    output_down: bool
    lengths: list[float]  # queue.length(m) для каждого из messages, в том же порядке
    completes: list[bool]  # queue.is_complete(m) для каждого из messages, в том же порядке
    autoplay: bool  # плеер сам начинает следующее и новое сообщение (контракт (6))


class Player:
    """Курсор (текущее сообщение + позиция) поверх очереди; команды кнопок из спеки.

    Играется файл queue.locate(current, позиция); доигравший файл сменяется следующим
    предложением сообщения. Сообщение кончилось и завершено (queue.is_complete) —
    прослушано, дальше следующее или первое непрослушанное; не завершено — статус
    playing без звука, пока не придёт предложение. На паузе и после stop автоперехода нет.
    Без автовоспроизведения (apply_prefs) плеер сам ничего не начинает: конец сообщения —
    stopped на следующем по порядку, новое сообщение в тишине только попадает в список.

    Ошибка файла — пропуск. Отказ вывода звука — тот же файл с той же позиции через
    RETRY_SECONDS, строка error — одна на отказ, а не на каждый повтор. Отказ кончается,
    когда звук пошёл (Playback.on_started); повтор, упавший до звука, — тот же отказ.

    Сообщение заводит задача хука (контракт (11)): она сразу уходит в очередь демона, а
    сообщение с её текстом — в список, ещё без звука; предложения приходят по id задачи
    демона (контракты (12), (13)). Начало проигрывания (_start: play/pause, автопереход,
    next/prev) сообщения, которое демон уже не озвучивает, сверяет его голос с текущим из
    config.json: устарел — кэш удаляется и текст уходит в демон заново (переозвучка),
    сообщение играет по мере прихода предложений, как растущее. Сообщение без звука
    переозвучивает только команда пользователя; автозапуск его пропускает (прослушано).

    Склейка играющего по предложениям сообщения подменяет их с той же позиции. Пока
    вывод недоступен, звука нет — повтор начнёт склейку с позиции, с которой запускался
    несыгравший файл (начало предложения или точка seek/продолжения в нём).
    На паузе и после stop склейка лишь запоминается: следующий запуск найдёт её locate.

    Приглушение (ducker, контракт (9)): статус стал playing — чужие потоки до prefs.duck %,
    пока playing — refresh раз в REFRESH_SECONDS, статус ушёл из playing и выход — restore.
    Ожидание предложения растущего сообщения — тоже playing: громкость не дрожит. Решает
    одно место — _follow_status из _changed, после которого видна каждая смена статуса.
    Удержание (hold, контракт (10)) — звучит свой текст: плеер на паузе, а приглушение идёт
    так же, как при playing; ушедший в hold из playing громкость не возвращает и не глушит
    заново. Звучит ли плеер для чужих потоков — _speaking, одно на duck и на сирот.
    Сироты (громкость приложения, поток которого закончился приглушённым): вне playing,
    пока ducker.has_orphans и не прошло WATCH_LIMIT_SECONDS с их последнего изменения
    (ducker.orphans_changed_at) — watch раз в WATCH_SECONDS; решает _follow_orphans рядом с
    _follow_status.
    """
    RETRY_SECONDS = 2
    REFRESH_SECONDS = 1
    WATCH_SECONDS = 2
    WATCH_LIMIT_SECONDS = 600  # опрос сирот вне речи — 10 минут после их последнего изменения

    def __init__(self, runtime_dir: Path, audio_sink: str | None,
                 history_dir: Path | None, history_db: Path | None,
                 ducker: Ducker | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 hook_tasks_dir: Path | None = None, *,
                 daemon_queue: DaemonQueue,
                 daemon_settings: DaemonSettings,
                 sessions: Sessions,
                 control_dir: Path | None = None) -> None:
        """ducker None — без приглушения чужих звуков; clock — часы Ducker.orphans_changed_at;
        hook_tasks_dir — queue/ хука (контракт (11)), None — без задач хука; daemon_queue —
        очередь демона (контракт (12)), daemon_settings — текущий голос для переозвучки;
        sessions — список сессий (загружен до start()), control_dir — control/ (контракт (14)),
        None — без запросов session add/remove."""
        self._clock = clock
        self.on_change: Callable[[PlayerState], None] = lambda state: None
        defaults = Prefs()
        self._queue = MessageQueue(defaults.tail)
        self._playback = Playback(audio_sink)
        self._playback.on_finished = self._on_finished
        self._playback.on_error = self._on_error
        self._playback.on_started = self._on_started
        self._inbox = Inbox(runtime_dir, self._on_sentence)
        self._saved = SavedMessages(Path(runtime_dir) / "tts-player" / "messages.json",
                                    self._inbox.store)
        self._daemon_queue = daemon_queue
        self._daemon_settings = daemon_settings
        self._sessions = sessions
        self._control = None
        if control_dir is not None:
            self._control = ControlRequests(control_dir, self.add_session, self.remove_session)
        self._hook_tasks = None
        if hook_tasks_dir is not None:
            self._hook_tasks = HookTasks(hook_tasks_dir, self._on_hook_task)
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
        self._autoplay = defaults.autoplay
        self._held = False  # звучит свой текст: сам ничего не начинаю (hold/release)
        self._ducker = ducker
        self._duck = defaults.duck
        self._ducking = False  # duck() вызван, restore() ещё нет
        self._refresh_id = 0
        self._watch_id = 0

    def apply_prefs(self, prefs: Prefs) -> None:
        """Настройки плеера: хвост очереди, автовоспроизведение, процент приглушения;
        играющее не трогается. Новый процент во время игры — restore, затем duck(новый)."""
        self._queue.tail = prefs.tail
        self._autoplay = prefs.autoplay
        if prefs.duck != self._duck:
            self._duck = prefs.duck
            self._unduck()  # _changed приглушит заново, если статус playing
        self._changed()

    def hold(self) -> None:
        """Свой текст начал звучать: playing → paused с запомненной позицией; пока держит,
        плеер сам не начинает ни следующее, ни новое сообщение. Команды пользователя
        работают как обычно."""
        self._held = True
        if self._status == "playing":
            self._position = self._now()
            self._halt()
            self._status = "paused"
        self._changed()

    def release(self) -> None:
        """Свой текст замолчал: удержание снято, статус не меняется."""
        self._held = False
        self._changed()

    def start(self) -> None:
        """История — раньше inbox/: накопленное в inbox/ начинает играть уже в start(),
        и склейка, записанная после первой строки playing, не должна считаться
        существовавшей до запуска. Колбэки обеих частей идут из цикла GLib, поэтому
        такой порядок ничего не теряет.

        Сообщения прошлого запуска (messages.json) восстанавливаются до inbox/ и задач хука:
        поздние предложения их задач — их, а не чужие; файлы store/, на которые они не
        ссылаются, удаляет Inbox. Непрослушанное играет само, если включено
        автовоспроизведение."""
        if self._history is not None:
            try:
                self._history.start()
            except OSError as error:
                log.warning("история %s недоступна (%s), работаю без склеек",
                            self._history_dir, error)
        self._queue.restore(self._saved.load())
        self._inbox.start(self._store_files())
        if self._control is not None:
            self._control.start()  # раньше задач хука: их сессии уже в списке
        if self._hook_tasks is not None:
            self._hook_tasks.start()
        if self._status == "idle" and self._starts_by_itself():
            message = self._queue.first_unheard()
            if message is not None:
                self._begin(message, requested=False)
        self._changed()

    def _store_files(self) -> frozenset[Path]:
        """Файлы store/, которые держат сообщения очереди: предложения и склейки."""
        files = {s.path for m in self._queue.messages for s in m.sentences}
        files.update(m.audio for m in self._queue.messages if m.audio is not None)
        return frozenset(files)

    def add_session(self, session_id: str, cwd: str) -> None:
        """Запрос `session add`; строку журнала (её читает e2e) пишет только настоящая смена."""
        if self._sessions.add(session_id, cwd):
            log.info("session add %s", session_id)

    def remove_session(self, session_id: str) -> None:
        """Запрос `session remove`; строка журнала — только при настоящей смене."""
        if self._sessions.remove(session_id):
            log.info("session remove %s", session_id)

    def _on_hook_task(self, task: HookTask) -> None:
        """Задача хука забрана (строка журнала — её читает e2e): её поля как есть уходят в
        очередь демона, сообщение с её текстом — в конец списка, ещё без звука."""
        log.info("task %s %s %d chars", task.id, task.session_id, len(task.text))
        message = Message(task.session_id, project=task.project, text=task.text,
                          persona=task.persona, speed=task.speed, received_at=task.timestamp)
        self._queue.append(message)
        self._send(message, task.fields, f"задача хука {task.id}")
        self._changed()

    def _send(self, message: Message, fields: dict, origin: str) -> None:
        """Задача демону для message; сбой записи — сообщение «не отправлено» и строка в
        журнал с origin и началом текста: задача не теряется молча."""
        task_id = self._daemon_queue.put(fields)
        message.sent = task_id is not None
        if task_id is None:
            log.warning("%s не отправлена в демон: %.*s", origin, LOGGED_TEXT_CHARS,
                        message.text or "")
            return
        self._queue.link(message, task_id)

    def shutdown(self) -> None:
        """Выход процесса: громкость чужих потоков — обратно; сообщения — в messages.json
        для следующего запуска (файлы непрослушанных остаются в store/, прослушанных —
        удаляются), вместе с id их задач: предложения, пришедшие после выхода, — их."""
        self._unduck()
        self._stop_watch()
        self._inbox.stop()
        if self._control is not None:
            self._control.stop()
        if self._hook_tasks is not None:
            self._hook_tasks.stop()
        if self._history is not None:
            self._history.stop()
        position = self._now()
        self._halt()
        self._saved.save(self._queue.release(self._current, position))

    def state(self) -> PlayerState:
        current = self._current
        length = 0.0 if current is None else self._queue.length(current)
        messages = self._queue.messages
        lengths = [self._queue.length(m) for m in messages]
        completes = [self._queue.is_complete(m) for m in messages]
        return PlayerState(self._status, current, self._now(), length,
                           messages, self._output_down, lengths, completes, self._autoplay)

    def toggle_play_pause(self) -> None:
        """Кнопка play/pause: playing — пауза с запомненной позицией; paused — с неё дальше;
        stopped и idle — текущее с курсора (курсор в конце — с начала), но доигранное
        текущее (прослушано, курсор в конце) уступает следующему по порядку, пришедшему
        после конца, — оно с начала; без текущего — первое непрослушанное с начала, нет
        его — ничего."""
        if self._status == "playing":
            self._position = self._now()
            self._halt()
            self._status = "paused"
        elif self._status == "paused":
            self._status = "playing"
            self._play_at(self._position)
        elif self._current is not None:
            following = self._following_finished()
            if following is None:
                self._resume()
            else:
                self._begin(following, requested=True)
        else:
            message = self._queue.first_unheard()
            if message is not None:
                self._begin(message, requested=True)
        self._changed()

    def _following_finished(self) -> Message | None:
        """Следующее по порядку за доигранным текущим (прослушано, курсор в конце); иначе None."""
        current = self._current
        if not current.heard or self._position < self._queue.length(current):
            return None
        return self._queue.following(current)

    def _resume(self) -> None:
        """Текущее с курсора после stop или конца; курсор в конце — с начала. Текущее,
        ставшее им без звука (конец сообщения без автовоспроизведения), входит в хвост здесь."""
        position = self._position
        if position >= self._queue.length(self._current):
            position = 0.0
        self._start(self._current, position, requested=True)

    def next(self) -> None:
        current = self._current
        following = None if current is None else self._queue.following(current)
        if following is not None:
            self._leave(current)
            self._begin(following, requested=True)
        self._changed()

    def prev(self) -> None:
        current = self._current
        preceding = None if current is None else self._queue.preceding(current)
        if preceding is not None:
            self._leave(current)
            self._begin(preceding, requested=True)
        self._changed()

    def play_message(self, message: Message) -> None:
        """Клик по строке списка: только выбор. Сообщение становится текущим с позиции 0 в
        статусе stopped и не начатым (в хвост входит при первом play/pause, как после
        `_stop_after`); звук играет, только когда пользователь нажмёт play/pause. Покинутое
        прослушано, только если начато и завершено (как у next/prev); клик по текущему и по
        вытесненному из очереди сообщению ничего не меняет."""
        if not any(message is queued for queued in self._queue.messages):
            return
        current = self._current
        if message is current:
            return
        if current is not None:
            self._leave(current)
        self._current = message
        self._sentence = None
        self._position = 0.0
        self._status = "stopped"
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
        if self._status == "idle" and self._starts_by_itself():
            message = self._queue.first_unheard()
            if message is not None:
                self._begin(message, requested=False)
        elif self._waiting():
            following = self._next_sentence()
            if following is not None:
                self._play_sentence(following)
            else:
                self._message_end()
        self._changed()

    def _on_entry(self, entry: HistoryEntry) -> None:
        message = self._queue.attach(entry, self._inbox.store)
        if message is None:
            return
        task_id = self._queue.task_of(message)
        if message.audio is None:
            log.info("ended %s %s", message.session_id, task_id)
        else:
            log.info("complete %s %s %.2fs", message.session_id, task_id, message.duration_s)
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
        # склейка, привязанная после конца по предложениям, может быть длиннее их суммы
        self._position = self._queue.length(current)
        if not self._starts_by_itself():
            self._stop_after(current)
            return
        upcoming = self._queue.following(current) or self._queue.first_unheard()
        if upcoming is None:
            self._status = "idle"
            return
        self._begin(upcoming, requested=False)

    def _starts_by_itself(self) -> bool:
        """Плеер сам начинает следующее и новое: автовоспроизведение включено и нет удержания."""
        return self._autoplay and not self._held

    def _stop_after(self, message: Message) -> None:
        """Конец сообщения без автовоспроизведения: stopped; текущим становится следующее по
        порядку с позиции 0, нет его — сообщение остаётся текущим с позицией в конце."""
        self._status = "stopped"
        following = self._queue.following(message)
        if following is None:
            return
        self._current = following
        self._sentence = None
        self._position = 0.0

    def _leave(self, message: Message) -> None:
        """Сообщение покинуто по next/prev/клику: звук стоп; прослушанным считается только
        начатое и завершённое — у растущего ещё придут предложения, а текущее, ставшее им
        без звука (конец сообщения без автовоспроизведения), не звучало."""
        self._halt()
        if self._queue.is_started(message) and self._queue.is_complete(message):
            self._queue.mark_heard(message)

    def _begin(self, message: Message, requested: bool) -> None:
        self._start(message, 0.0, requested)

    def _start(self, message: Message, position: float, requested: bool) -> None:
        """Начало проигрывания — одно место для play/pause, автоперехода и next/prev:
        сообщение входит в хвост; кэш устарел — переозвучка с начала. requested — команда
        пользователя (play/pause, next/prev); только она переозвучивает сообщение без звука,
        автозапуск его пропускает (_play_at: прослушано, дальше следующее)."""
        self._queue.start(message)
        self._current = message
        self._status = "playing"
        if self._revoice_if_stale(message, requested):
            position = 0.0
        self._play_at(position)

    def _revoice_if_stale(self, message: Message, requested: bool) -> bool:
        """Сообщение, которое демон уже не озвучивает (is_complete), с голосом, отличным от
        текущего, или без звука (только requested): файлы удалены, текст — в демон текущим
        голосом. True — переозвучка начата (и при сбое записи задачи: старого звука уже нет).
        Сбой чтения config.json — кэш считается актуальным, строка в журнал."""
        if not self._queue.is_complete(message):
            return False
        silent = not message.sentences and message.audio is None
        if silent and not requested:
            return False
        voice = self._active_voice()
        if voice is None:
            return False
        if not silent and (message.persona, message.speed) == (voice.persona, voice.speed):
            return False
        log.info("revoice %s %s: %s %s -> %s %s", message.session_id,
                 self._queue.task_of(message), message.persona, message.speed,
                 voice.persona, voice.speed)
        self._queue.forget_audio(message)
        message.persona = voice.persona
        message.speed = voice.speed
        fields = {**voice.fields, "text": message.text, "session_id": message.session_id,
                  "project": message.project}
        self._send(message, fields, "переозвучка")
        return True

    def _active_voice(self) -> ActiveVoice | None:
        try:
            return self._daemon_settings.active_voice()
        except SettingsError as error:
            log.warning("голос не прочитан (%s), кэш считается актуальным", error)
            return None

    def _play_at(self, position: float) -> None:
        current = self._current
        if current.audio is None and not current.sentences:
            # звука ещё нет: ждём предложение, как у растущего; не ждём — конец сообщения
            self._file = None
            self._sentence = None
            self._position = 0.0
            self._message_end()
            return
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

    def _next_sentence(self) -> Sentence | None:
        """Что играть ожидающему сообщению: следующее за доигравшим, а если ещё ничего не
        играло (сообщение без звука при начале) — первое."""
        if self._sentence is None:
            return next(iter(self._current.sentences), None)
        return self._sentence_after(self._sentence)

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

    def _follow_status(self) -> None:
        """Приглушение по статусу: начал звучать — duck и таймер refresh, замолчал — restore."""
        if self._ducker is None:
            return
        playing = self._speaking()
        if playing and not self._ducking:
            self._ducker.duck(self._duck)
            self._ducking = True
            self._refresh_id = GLib.timeout_add_seconds(self.REFRESH_SECONDS, self._refresh)
        elif not playing:
            self._unduck()

    def _speaking(self) -> bool:
        """Для чужих потоков плеер звучит: играет сам или держит паузу ради своего текста."""
        return self._status == "playing" or self._held

    def _follow_orphans(self) -> None:
        """Таймер watch: идёт вне playing, пока есть сироты; иначе снят."""
        if self._ducker is None:
            return
        wanted = not self._speaking() and self._watching_orphans()
        if wanted and not self._watch_id:
            self._watch_id = GLib.timeout_add_seconds(self.WATCH_SECONDS, self._watch)
        elif not wanted:
            self._stop_watch()

    def _watch(self) -> bool:
        self._ducker.watch()
        if self._watching_orphans():
            return GLib.SOURCE_CONTINUE
        self._watch_id = 0
        return GLib.SOURCE_REMOVE

    def _watching_orphans(self) -> bool:
        """Сироты есть и менялись не позже WATCH_LIMIT_SECONDS назад; дальше — при начале речи."""
        changed_at = self._ducker.orphans_changed_at
        if not self._ducker.has_orphans or changed_at is None:
            return False
        return self._clock() - changed_at < self.WATCH_LIMIT_SECONDS

    def _stop_watch(self) -> None:
        if self._watch_id:
            GLib.source_remove(self._watch_id)
            self._watch_id = 0

    def _refresh(self) -> bool:
        self._ducker.refresh()
        return GLib.SOURCE_CONTINUE

    def _unduck(self) -> None:
        """Снять таймер refresh и вернуть громкость; не приглушено — ничего."""
        if not self._ducking:
            return
        GLib.source_remove(self._refresh_id)
        self._refresh_id = 0
        self._ducking = False
        self._ducker.restore()

    def _changed(self) -> None:
        """Каждая смена статуса доходит сюда: сначала приглушение по статусу, затем окно."""
        self._follow_status()
        self._follow_orphans()
        try:
            self.on_change(self.state())
        except Exception:
            log.exception("on_change: ошибка обработчика окна")
