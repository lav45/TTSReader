"""Плеер-контроллер: курсор по очереди, команды кнопок, снимок состояния для окна."""
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Literal

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from tts_player.control import ControlRequests  # noqa: E402
from tts_player.daemon_queue import DaemonQueue  # noqa: E402
from tts_player.daemon_settings import ActiveVoice, DaemonSettings  # noqa: E402
from tts_player.inbox import Inbox  # noqa: E402
from tts_player.hook_tasks import HookTasks  # noqa: E402
from tts_player.model import UNKNOWN_TASK, HookTask, Message, Sentence  # noqa: E402
from tts_player.mpris_pause import MprisPause  # noqa: E402
from tts_player.output_idle import OutputIdle  # noqa: E402
from tts_player.playback import Playback  # noqa: E402
from tts_player.player_settings import Prefs  # noqa: E402
from tts_player.queue import MessageQueue, QueueSnapshot, TaskOrder  # noqa: E402
from tts_player.saved_messages import SavedMessages  # noqa: E402
from tts_player.sentence_counter import SentenceCounter  # noqa: E402
from tts_player.sessions import Session, Sessions  # noqa: E402
from tts_player.settings_error import SettingsError  # noqa: E402

log = logging.getLogger("tts_player.player")

Status = Literal["playing", "paused", "stopped", "idle"]

LOGGED_TEXT_CHARS = 80  # столько знаков текста задачи в строке о сбое её записи


@dataclass(frozen=True)
class PlayerState:
    """Снимок для окна. Сообщения и курсор — очереди активной сессии (нет активной —
    сообщений нет). idle — ничего не играет: current доигран либо ещё не начат
    (пока звучит свой текст), или None — фокуса нет (сообщений нет, папка только открыта)."""
    status: Status
    current: Message | None
    position_s: float  # внутри текущего сообщения
    length_s: float  # queue.length(current), 0 без текущего
    messages: list[Message]
    output_down: bool
    lengths: list[float]  # queue.length(m) для каждого из messages, в том же порядке
    sessions: list[Session]  # порядок списка (Sessions.sessions)
    active: str | None  # активная сессия; None — не выбрана
    autoplay: bool = True  # кнопка автовоспроизведения: последний apply_prefs


class Player:
    """Курсор (текущее сообщение + позиция) поверх очереди; команды кнопок из спеки.

    Играется файл queue.locate(current, позиция); доигравший файл сменяется следующим
    предложением сообщения. Сообщение кончилось и завершено (queue.is_complete) —
    прослушано, дальше следующее по порядку; за последним — idle, и первое предложение нового
    сообщения начинает само это сообщение, а не более ранние непрослушанные (накопленные в
    закрытой папке, отложенные): автопереход идёт только вперёд; не завершено — статус
    playing без звука, пока не придёт предложение. На паузе и после stop автоперехода нет.
    play/pause без текущего начинает последнее сообщение очереди.

    Длина истории — по фокусу (_focused): фокус у очереди активной сессии, пока текущее
    звучит, стоит на паузе или выбрано и не запущено (stopped), а в idle — пока есть
    отданный демону в этом запуске ответ, до которого дойдёт автопереход (новее текущего,
    ещё не завершён); у закрытых папок фокуса нет. Без фокуса очередь режется до tail
    (MessageQueue.trim): старшие — прослушанные, непрослушанные, отложенные — удаляются с
    файлами; текущее места в tail не занимает и остаётся. С фокусом сообщения копятся, позади
    курсора начатые ограничены tail (MessageQueue.start). Режет одно место — _trim_unfocused
    из _changed (новое сообщение, конец последнего, открытие и закрытие папки, смена tail) и
    из start() после восстановления messages.json.
    Открытая (активная) сессия играет подряд, пока нажата кнопка автовоспроизведения
    (prefs.autoplay, контракт (6)). Плеер сам начинает (_starts_by_itself), только когда
    кнопка нажата и свой текст не звучит (hold); иначе конец сообщения — stopped на следующем
    по порядку, новое сообщение в тишине только попадает в список, задача хука откладывается
    (deferred), а очередь без фокуса режется до tail, как у закрытой папки. Отжатие звучащее
    не обрывает: оно доигрывает и встаёт; нажатие звук не начинает, отложенные остаются
    отложенными.
    Кнопка ⏹ (stop) снимает курсор: звук стоп, текущего нет, idle, как после конца
    последнего; при нажатой кнопке сам начнётся ответ новее снятого, а снятое — нет, пока
    не начата или не выбрана дорожка (_stopped, _after_stopped).

    Ошибка файла — пропуск. Отказ вывода звука — тот же файл с той же позиции через
    RETRY_SECONDS, строка error — одна на отказ, а не на каждый повтор. Отказ кончается,
    когда звук пошёл (Playback.on_started); повтор, упавший до звука, — тот же отказ.

    Сессии (Sessions, вход «сессии»): у каждой сессии из списка своя MessageQueue — свой
    хвост, — все над одним TaskOrder: демон синтезирует задачи всех сессий по одной.
    Курсор ходит по очереди активной сессии (_queue); нет активной — по пустой очереди.
    Предложение (13) идёт в очередь, которой принадлежит его задача (MessageQueue.owns);
    чужое предложение удаляет играемая очередь.

    Сообщение заводит задача хука (контракт (11)) своей сессии: сессия активна и свой текст не
    звучит — задача сразу уходит в очередь демона (12), иначе сообщение отложено (deferred) —
    только текст; сессии нет в списке — задача удалена. Число предложений текста
    (sentence_count, по нему конец сообщения — MessageQueue.is_complete) считает
    SentenceCounter один раз на сообщение; ответ асинхронный (_on_count), и пришедший после
    конца полученного завершает ожидающее сообщение. Сообщение с текстом — в конец своей
    очереди, ещё без звука; предложения приходят по id задачи демона (13). Начало
    проигрывания (_start: play/pause, автопереход) отложенного сообщения отдаёт
    его текст демону текущим голосом; сообщения, которое демон уже не озвучивает, — сверяет
    его голос с текущим из config.json: устарел — кэш удаляется и текст уходит в демон
    заново (переозвучка). В обоих случаях сообщение играет по мере прихода предложений, как
    растущее. Сообщение без звука (не отложенное) переозвучивает только команда пользователя;
    автозапуск его пропускает (прослушано).

    Пауза других плееров (pause, контракт (9')): плеер начал звучать (_speaking стал True) —
    pause.pause_playing, и звук ждёт ответа: кто-то встал на паузу — первый звук через
    PAUSE_GAP_SECONDS, никто — сразу. Замолчал, и пауза кого-то держит, — pause.resume через
    RESUME_DELAY_SECONDS тишины; речь снова до них — таймер снят, новой паузы нет, звук сразу
    (если 3 с уже прошли). Ожидание предложения растущего сообщения — тоже playing: другие
    стоят. Удержание (hold, контракт (10)) — звучит свой текст: плеер на паузе, а для других
    звучит (_speaking); ушедший в hold из playing паузу не повторяет, а 3 с у своего текста нет:
    его звук идёт своим Playback, ворота держат только звук этого плеера. Замолчал до ответа
    pause_playing — продолжение решает ответ. Решает одно место — _follow_status из _changed,
    после которого видна каждая смена статуса.
    Флажок pause_others (apply_prefs, контракт (6)): снят — речь других не трогает, звук сразу
    (без pause_playing и без 3 с). Снят, пока другие стоят, — они продолжают сразу
    (pause.resume без RESUME_DELAY_SECONDS), таймер продолжения и тишина 3 с сняты; снят, пока
    ответ pause_playing не пришёл, — по ответу они продолжают сразу, без 3 с. Поставлен —
    пауза со следующего начала речи, идущей речи не касается. То же значение ничего не меняет.
    Ворота звука: _start_file только запоминает файл и позицию (_due), Playback.play зовёт
    _follow_status, когда ответа не ждут и 3 с не идут. Поэтому команды в эти 3 с (⏸, клик и
    ▶, перемотка) меняют, что заиграет, а не когда: отсчёт не перезапускается, другие уже
    стоят; окно видит playing на позиции курсора.
    """
    RETRY_SECONDS = 2
    PAUSE_GAP_SECONDS = 3  # тишина перед первым словом, если кто-то встал на паузу
    RESUME_DELAY_SECONDS = 5  # тишина плеера, после которой другие продолжают

    def __init__(self, runtime_dir: Path, audio_sink: str | None,
                 pause: MprisPause | None = None,
                 hook_tasks_dir: Path | None = None, *,
                 daemon_queue: DaemonQueue,
                 daemon_settings: DaemonSettings,
                 sessions: Sessions,
                 control_dir: Path | None = None,
                 sentence_counter: SentenceCounter | None = None,
                 output: OutputIdle | None = None) -> None:
        """pause None — других плееров не трогать; плеер её владелец (останавливает в shutdown);
        hook_tasks_dir — queue/ хука (контракт (11)), None — без задач хука; daemon_queue —
        очередь демона (контракт (12)), daemon_settings — текущий голос для переозвучки;
        sessions — список сессий, уже загруженный: его сессиям очереди заводятся здесь, а
        сообщения прошлого запуска раскладывает по ним start(); control_dir — control/
        (контракт (14)), None — без запросов session add/remove;
        sentence_counter — счётчик предложений, плеер его владелец (останавливает в shutdown);
        None — число предложений неизвестно, конец сообщения по запасным признакам;
        output — простой вывода, общий со своим текстом (Playback), None — без тишины
        пробуждения."""
        self.on_change: Callable[[PlayerState], None] = lambda state: None
        defaults = Prefs()
        self._tail = defaults.tail
        self._task_order = TaskOrder()  # общий порядок задач демона для очередей сессий
        self._sessions = sessions
        # по session_id, сессии из Sessions; нет активной — курсор ходит по пустой
        self._queues = {session.id: self._new_queue() for session in sessions.sessions}
        self._no_session = self._new_queue()
        self._playback = Playback(audio_sink, output)
        self._playback.on_finished = self._on_finished
        self._playback.on_error = self._on_error
        self._playback.on_started = self._on_started
        self._inbox = Inbox(runtime_dir, self._on_sentence)
        self._saved = SavedMessages(Path(runtime_dir) / "tts-player" / "messages.json",
                                    self._inbox.store)
        self._daemon_queue = daemon_queue
        self._daemon_settings = daemon_settings
        self._counter = sentence_counter
        self._control = None
        if control_dir is not None:
            self._control = ControlRequests(control_dir, self.add_session, self.remove_session,
                                            self.clear_session)
        self._hook_tasks = None
        if hook_tasks_dir is not None:
            self._hook_tasks = HookTasks(hook_tasks_dir, self._on_hook_task)
        self._status: Status = "idle"
        self._current: Message | None = None
        self._stopped: Message | None = None  # снят кнопкой ⏹: сам не начинается (stop)
        # Позиция, с которой запущен _file (без файла — курсор); _file_base — начало
        # _file в сообщении. _sentence — предложение _file или последнее доигравшее.
        self._position = 0.0
        self._file: Path | None = None
        self._file_base = 0.0
        self._sentence: Sentence | None = None
        self._output_down = False
        self._retry_id = 0
        self._held = False  # звучит свой текст: сам ничего не начинаю (hold/release)
        self._autoplay = defaults.autoplay  # кнопка автовоспроизведения (apply_prefs)
        self._due = False  # _file запомнен, Playback.play ещё не звали (ворота звука)
        self._pause = pause
        self._spoke = False  # _speaking() на прошлом _follow_status
        self._pausing = False  # pause_playing вызван, ответа ещё нет
        self._gap_id = 0  # таймер PAUSE_GAP_SECONDS: идёт — звук ждёт
        self._resume_id = 0  # таймер RESUME_DELAY_SECONDS до pause.resume
        self._pause_others = defaults.pause_others  # флажок паузы других (apply_prefs)
        # cwd очищенной `/clear` активной сессии: сессия оттуда станет активной вместо неё.
        # Инвариант: ожидание замены => активной сессии нет (ставится только при снятии
        # активной, любая активация его снимает)
        self._replace_cwd: str | None = None
        # Отданные демону в этом запуске (_send) и ещё живые в очередях: только они, пока
        # синтезируются, дают фокус в idle (_focused); восстановленные из messages.json — нет
        self._sent_in_run: set[Message] = set()

    def apply_prefs(self, prefs: Prefs) -> None:
        """Настройки плеера: хвост очереди каждой сессии, кнопка автовоспроизведения, флажок
        паузы других плееров; играющее не трогается. Флажок снят — другие продолжают сразу, звук
        без тишины 3 с (_follow_status); поставлен — пауза со следующего начала речи. Очереди без фокуса режутся до нового tail сразу, с фокусом —
        хвост начатых постепенно (MessageQueue.start); отжатая кнопка снимает фокус (_focused),
        и очередь режется тут же. Нажатие звук не начинает: отсюда путь только к обрезке и
        окну (_changed)."""
        self._tail = prefs.tail
        self._autoplay = prefs.autoplay
        self._pause_others = prefs.pause_others
        for queue in [*self._queues.values(), self._no_session]:
            queue.tail = prefs.tail
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
        """Сообщения прошлого запуска (messages.json) восстанавливаются до inbox/ и задач хука,
        каждое — в очередь своей сессии; сообщения сессий не из списка не восстанавливаются.
        Поздние предложения восстановленных задач — их, а не чужие; файлы store/, на которые
        сообщения не ссылаются, удаляет Inbox. Сам плеер ничего не начинает: открытая папка
        ждёт (idle, текущего нет), восстановленные очереди режутся до tail; новые ответы
        играют как обычно. Восстановленным без числа предложений оно запрашивается."""
        self._restore(self._saved.load())
        self._trim_unfocused()
        for queue in self._queues.values():
            for message in queue.messages:
                if message.sentence_count is None:
                    self._count(message)
        self._inbox.start(self._store_files())
        if self._control is not None:
            self._control.start()  # раньше задач хука: их сессии уже в списке
        if self._hook_tasks is not None:
            self._hook_tasks.start()
        self._changed()

    def _new_queue(self) -> MessageQueue:
        return MessageQueue(self._tail, self._task_order)

    def _restore(self, snapshot: QueueSnapshot) -> None:
        """Снимок прошлого запуска — по очередям сессий из списка; сообщения прочих сессий
        забываются (их файлы удалит Inbox: на них никто не ссылается)."""
        for session_id, queue in self._queues.items():
            messages = [m for m in snapshot.messages if m.session_id == session_id]
            started = [m for m in snapshot.started if m.session_id == session_id]
            queue.restore(QueueSnapshot(messages, started, snapshot.task_order,
                                        snapshot.delivered))
        for session_id in {m.session_id for m in snapshot.messages} - self._queues.keys():
            log.info("сообщения сессии %s не восстановлены: её нет в списке", session_id)

    @property
    def _queue(self) -> MessageQueue:
        """Очередь, по которой ходит курсор: активной сессии; нет активной — пустая."""
        return self._queues.get(self._sessions.active, self._no_session)

    def _store_files(self) -> frozenset[Path]:
        """Файлы store/, которые держат сообщения очередей сессий: их предложения."""
        messages = [m for queue in self._queues.values() for m in queue.messages]
        return frozenset(s.path for m in messages for s in m.sentences)

    def add_session(self, session_id: str, cwd: str) -> None:
        """Запрос `session add`: у новой сессии пустая очередь; активной сама не становится
        (её открывает пользователь), кроме замены очищенной `/clear` активной из того же cwd:
        тогда она активна как после клика по папке (текущего нет, idle), ожидание снято. Строки
        журнала (их читает e2e) пишет только настоящая смена."""
        if not self._sessions.add(session_id, cwd):
            return
        log.info("session add %s", session_id)
        self._queues[session_id] = self._new_queue()
        if cwd == self._replace_cwd:
            self._replace_cwd = None
            self._activate(session_id)
            log.info("session replace %s", session_id)
        self._changed()

    def remove_session(self, session_id: str) -> None:
        """Запрос `session remove` (хук `SessionEnd`): файлы и тексты сессии удалены, её поздние
        предложения — чужие. Была активной — звук стоп, активной нет, пока пользователь не
        выберет сессию: текущего нет, статус idle. Замены не ждёт. Строка журнала — только при
        настоящей смене."""
        if self._end_session(session_id) is not None:
            log.info("session remove %s", session_id)
            self._changed()

    def clear_session(self, session_id: str, cwd: str) -> None:
        """Запрос `session clear` (`/clear` в Claude Code): сессия кончилась как при remove.
        Была активной — следующая сессия из cwd станет активной вместо неё (add_session).
        Нет в списке — ничего. Строка журнала — только при настоящей смене."""
        was_active = self._end_session(session_id)
        if was_active is None:
            return
        log.info("session clear %s", session_id)
        if was_active:
            self._replace_cwd = cwd
        self._changed()

    def _end_session(self, session_id: str) -> bool | None:
        """Общая часть remove и clear: сессия и её очередь убраны, была активной — звук стоп,
        курсор сброшен. None — сессии нет в списке, было ли активной — иначе."""
        was_active = session_id == self._sessions.active
        if not self._sessions.remove(session_id):
            return None
        if was_active:
            self._halt()
            self._reset_cursor()
        self._queues.pop(session_id).clear()
        return was_active

    def select_session(self, session_id: str) -> None:
        """Клик по закрытой папке: активная или не из списка — ничего. Иначе звук стоп, она
        активна, фокуса нет (текущего нет, idle): играть начнёт ▶, клик по строке или её новое
        сообщение. Выбор пользователя снимает ожидание замены после `/clear`."""
        if session_id == self._sessions.active or not self._sessions.has(session_id):
            return
        self._replace_cwd = None
        self._activate(session_id)
        log.info("session select %s", session_id)
        self._changed()

    def _activate(self, session_id: str) -> None:
        """Сессия из списка становится активной: звук стоп, фокуса нет (текущего нет, idle)."""
        self._halt()
        self._sessions.select(session_id)
        self._reset_cursor()

    def close_session(self) -> None:
        """Клик по открытой папке: звук стоп, активной нет, пока пользователь не выберет
        сессию; её сообщения и файлы остаются. Без активной — ничего."""
        session_id = self._sessions.active
        if not self._sessions.deselect():
            return
        log.info("session close %s", session_id)
        self._halt()
        self._reset_cursor()
        self._changed()

    def _on_hook_task(self, task: HookTask) -> None:
        """Задача хука забрана. Сначала — запросы control/, лежащие сейчас:
        `tts-player-hook start` пишет add раньше, чем немедленная озвучка — задачу. Сессии
        нет в списке — задача удалена (строка «dropped»). Иначе (строка «chars» — её читает e2e)
        сообщение с её текстом — в конец очереди сессии, ещё без звука; сессия активна и
        плеер ведёт сам (признак _leads_itself: кнопка автовоспроизведения нажата,
        свой текст не звучит) — поля задачи как есть уходят в очередь демона, иначе сообщение
        отложено (строка «deferred») до play/pause. Число предложений текста запрашивается после
        отправки (сбой счёта её не задерживает), и у отложенного тоже: текст не меняется и
        при переозвучке."""
        if self._control is not None:
            self._control.drain()
        session_id = task.session_id
        if not self._sessions.has(session_id):
            log.info("task %s %s dropped: not in list", task.id, session_id)
            return
        log.info("task %s %s %d chars", task.id, session_id, len(task.text))
        message = Message(session_id, project=task.project, text=task.text,
                          persona=task.persona, speed=task.speed, received_at=task.timestamp)
        queue = self._queues[session_id]
        queue.append(message)
        if session_id == self._sessions.active and self._leads_itself():
            self._send(queue, message, task.fields, f"задача хука {task.id}")
        else:
            message.sent = False
            message.deferred = True
            log.info("task %s %s deferred", task.id, session_id)
        self._count(message)  # после отправки: сбой счёта задачу демону не задерживает
        self._changed()

    def _send(self, queue: MessageQueue, message: Message, fields: dict, origin: str) -> None:
        """Задача демону для message очереди queue; отложенное перестаёт им быть. Сбой
        записи — сообщение «не отправлено» и строка в журнал с origin и началом текста:
        задача не теряется молча."""
        task_id = self._daemon_queue.put(fields)
        message.sent = task_id is not None
        if task_id is None:
            log.warning("%s не отправлена в демон: %.*s", origin, LOGGED_TEXT_CHARS,
                        message.text or "")
            return
        message.deferred = False
        queue.link(message, task_id)
        self._sent_in_run.add(message)

    def _count(self, message: Message) -> None:
        """Запрос числа предложений текста message; ответ — в _on_count. Без счётчика или
        текста — ничего: число остаётся неизвестным."""
        if self._counter is None or message.text is None:
            return
        self._counter.count(message.text, lambda count: self._on_count(message, count))

    def _on_count(self, message: Message, count: int) -> None:
        """Число предложений пришло. Текущее, доигранное до конца полученного и ждущее
        предложения, могло этим завершиться: тогда — конец сообщения."""
        message.sentence_count = count
        if message is self._current:
            self._end_if_waiting()
        self._changed()

    def shutdown(self) -> None:
        """Выход процесса: другие плееры продолжают сразу, без RESUME_DELAY_SECONDS; счётчик
        предложений остановлен; сообщения — в messages.json для следующего запуска (файлы
        непрослушанных остаются в store/, прослушанных — удаляются), вместе с id их задач:
        предложения, пришедшие после выхода, — их."""
        if self._gap_id:
            GLib.source_remove(self._gap_id)
            self._gap_id = 0
        if self._resume_id:
            self._cancel_resume()
        if self._pause is not None:
            self._pause.resume()
            self._pause.stop()
        if self._counter is not None:
            self._counter.stop()
        self._inbox.stop()
        if self._control is not None:
            self._control.stop()
        if self._hook_tasks is not None:
            self._hook_tasks.stop()
        position = self._now()
        self._halt()
        self._saved.save(self._release(position))

    def _release(self, position: float) -> QueueSnapshot:
        """Снимки очередей всех сессий одним снимком (сообщение помнит свою сессию); текущее
        с позицией — только у играемой. Порядок задач общий: delivered у снимков один."""
        messages, started, task_order, delivered = [], [], {}, 0
        for queue in self._queues.values():
            current = self._current if queue is self._queue else None
            snapshot = queue.release(current, position)
            messages += snapshot.messages
            started += snapshot.started
            task_order.update(snapshot.task_order)
            delivered = max(delivered, snapshot.delivered)
        return QueueSnapshot(messages, started, task_order, delivered)

    def state(self) -> PlayerState:
        current = self._current
        length = 0.0 if current is None else self._queue.length(current)
        messages = self._queue.messages
        lengths = [self._queue.length(m) for m in messages]
        sessions = self._sessions.sessions
        return PlayerState(self._status, current, self._now(), length,
                           messages, self._output_down, lengths,
                           sessions, self._sessions.active, self._autoplay)

    def toggle_play_pause(self) -> None:
        """Кнопка play/pause: playing — пауза с запомненной позицией; paused — с неё дальше;
        stopped и idle — текущее с курсора (курсор в конце — с начала), но доигранное
        текущее (прослушано, курсор в конце) уступает следующему по порядку, пришедшему
        после конца, — оно с начала; без текущего — последнее сообщение очереди с начала
        (отложенное озвучивается), очередь пуста — ничего."""
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
            messages = self._queue.messages
            if messages:
                self._begin(messages[-1], requested=True)
        self._changed()

    def _following_finished(self) -> Message | None:
        """Следующее по порядку за доигранным текущим (прослушано, курсор в конце); иначе None."""
        current = self._current
        if not current.heard or self._position < self._queue.length(current):
            return None
        return self._queue.following(current)

    def _resume(self) -> None:
        """Текущее с курсора после stop или конца; курсор в конце — с начала. Текущее,
        ставшее им без звука (конец сообщения, пока звучал свой текст), входит в хвост здесь."""
        position = self._position
        if position >= self._queue.length(self._current):
            position = 0.0
        self._start(self._current, position, requested=True)

    def play_message(self, message: Message) -> None:
        """Клик по строке списка: только выбор. Сообщение становится текущим с позиции 0 в
        статусе stopped и не начатым (в хвост входит при первом play/pause, как после
        `_stop_after`); звук играет, только когда пользователь нажмёт play/pause. Покинутое
        прослушано, только если начато и завершено (как в _leave); клик по текущему и по
        вытесненному из очереди сообщению ничего не меняет."""
        if not any(message is queued for queued in self._queue.messages):
            return
        current = self._current
        if message is current:
            return
        if current is not None:
            self._leave(current)
        self._current = message
        self._stopped = None
        self._sentence = None
        self._position = 0.0
        self._status = "stopped"
        self._output_down = False  # повтор снят _leave: отказ вывода больше не показывается
        self._changed()

    def stop(self) -> None:
        """Кнопка ⏹: звук стоп, текущего нет, idle — то же состояние, что после конца
        последнего сообщения: при нажатой кнопке автовоспроизведения сам начнётся ответ новее
        снятого (_after_stopped), очередь без фокуса режется до tail. Снятое прослушано по
        правилу клика на другую строку (_leave); само оно не начинается, пока не начата или
        не выбрана дорожка. Без текущего — ничего."""
        current = self._current
        if current is None:
            return
        self._leave(current)
        self._reset_cursor()
        self._stopped = current
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
        """«Очистить» активную сессию: звук стоп, её файлы и тексты удалены, сессия остаётся;
        новые предложения играют как обычно. Очереди других сессий не трогаются."""
        self._halt()
        self._queue.clear()
        self._reset_cursor()
        self._changed()

    def _reset_cursor(self) -> None:
        """Текущего нет, статус idle (после _halt): очередь очищена или сессии больше нет."""
        self._current = None
        self._stopped = None
        self._sentence = None
        self._position = 0.0
        self._status = "idle"
        self._output_down = False

    def _on_sentence(self, sentence: Sentence) -> None:
        """Предложение — в очередь, которой принадлежит его задача; ничья — играемой, она
        удаляет его как чужое. Предложение другой сессии не играет, но может завершить
        ожидающее текущее: порядок задач общий. Предложение без id задачи (обёртка не нашла
        current_message в playback.json демона) относится к задаче своего прохода синтеза, если
        у какого-то сообщения уже есть предложение с той же меткой прохода."""
        sentence = self._claim_unknown(sentence)
        queue = next((q for q in self._queues.values() if q.owns(sentence.task_id)), self._queue)
        current = self._current
        playing_it = current is not None and sentence.task_id in current.task_ids
        queue.add(sentence, sounded=self._sentence if playing_it else None)
        if queue is not self._queue:
            self._end_if_waiting()
        elif self._status == "idle" and self._starts_by_itself():
            message = self._queue.message_of(sentence.task_id)
            if message is not None and not message.heard and self._after_stopped(message):
                self._begin(message, requested=False)
        elif self._waiting():
            following = self._next_sentence()
            if following is not None:
                self._play_sentence(following)
            else:
                self._message_end()
        self._changed()

    def _after_stopped(self, message: Message) -> bool:
        """Сообщение новее снятого кнопкой ⏹ (или снятого нет, или его уже нет в очереди):
        автопереход в idle идёт только вперёд от остановленного."""
        messages = self._queue.messages
        stopped = self._stopped
        if not any(stopped is m for m in messages):
            return True
        return messages.index(message) > messages.index(stopped)

    def _claim_unknown(self, sentence: Sentence) -> Sentence:
        if sentence.task_id != UNKNOWN_TASK:
            return sentence
        task_id = next((t for q in self._queues.values()
                        if (t := q.task_of_pass(sentence.session_id, sentence.token))), None)
        if task_id is None:
            return sentence
        log.info("предложение без id задачи отнесено к задаче %s по проходу %s: %s", task_id,
                 sentence.token, sentence.path.name)
        return replace(sentence, task_id=task_id)

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
        log.info("ended %s %s", current.session_id, self._queue.task_of(current))
        self._queue.mark_heard(current)
        if not self._starts_by_itself():
            self._stop_after(current)
            return
        upcoming = self._queue.following(current)
        if upcoming is None:
            self._status = "idle"
            return
        self._begin(upcoming, requested=False)

    def _starts_by_itself(self) -> bool:
        """Плеер сам начинает следующее и новое: кнопка автовоспроизведения нажата и свой
        текст не звучит (нет удержания). Решает конец сообщения и первое предложение в idle;
        задачу хука решает тот же признак напрямую (_leads_itself)."""
        return self._leads_itself()

    def _leads_itself(self) -> bool:
        """Плеер ведёт сам, новое отдаётся демону сразу: кнопка автовоспроизведения нажата и
        свой текст не звучит (нет удержания). Единственное место этого условия: по нему
        _starts_by_itself решает переходы, а _on_hook_task — отправку или отсрочку задачи."""
        return self._autoplay and not self._held

    def _stop_after(self, message: Message) -> None:
        """Конец сообщения, когда плеер сам не начинает (кнопка автовоспроизведения отжата
        или звучит свой текст): stopped; текущим становится следующее по порядку с позиции 0,
        нет его — сообщение остаётся текущим с позицией в конце."""
        self._status = "stopped"
        following = self._queue.following(message)
        if following is None:
            return
        self._current = following
        self._sentence = None
        self._position = 0.0

    def _leave(self, message: Message) -> None:
        """Сообщение покинуто по клику на другую строку: звук стоп; прослушанным считается только
        начатое и завершённое — у растущего ещё придут предложения, а текущее, ставшее им
        без звука (конец сообщения, пока звучал свой текст), не звучало."""
        self._halt()
        if self._queue.is_started(message) and self._queue.is_complete(message):
            self._queue.mark_heard(message)

    def _begin(self, message: Message, requested: bool) -> None:
        self._start(message, 0.0, requested)

    def _start(self, message: Message, position: float, requested: bool) -> None:
        """Начало проигрывания — одно место для play/pause и автоперехода:
        сообщение входит в хвост; отложенное или с устаревшим кэшем — озвучка с начала.
        requested — команда пользователя (play/pause); только она переозвучивает
        сообщение без звука, автозапуск его пропускает (_play_at: прослушано, дальше
        следующее), кроме отложенного: его текст демону ещё не отдавался."""
        self._queue.start(message)
        self._current = message
        self._stopped = None
        self._status = "playing"
        if self._voice_if_due(message, requested):
            position = 0.0
        self._play_at(position)

    def _voice_if_due(self, message: Message, requested: bool) -> bool:
        """Сообщение, которое демон уже не озвучивает (is_complete), с голосом, отличным от
        текущего, или без звука (отложенное — всегда, прочее — только requested): файлы
        удалены, текст — в демон текущим голосом. True — озвучка начата (и при сбое записи
        задачи: старого звука уже нет). Сбой чтения config.json — кэш считается актуальным,
        строка в журнал; отложенное тогда остаётся без звука и пропускается, как не
        отправленное."""
        if not self._queue.is_complete(message):
            return False
        silent = not message.sentences
        if silent and not requested and not message.deferred:
            return False
        voice = self._active_voice(message)
        if voice is None:
            return False
        if not silent and (message.persona, message.speed) == (voice.persona, voice.speed):
            return False
        if message.deferred:
            log.info("voice deferred %s: %s %s", message.session_id, voice.persona, voice.speed)
            origin = "отложенное сообщение"
        else:
            log.info("revoice %s %s: %s %s -> %s %s", message.session_id,
                     self._queue.task_of(message), message.persona, message.speed,
                     voice.persona, voice.speed)
            origin = "переозвучка"
        self._queue.forget_audio(message)
        message.persona = voice.persona
        message.speed = voice.speed
        fields = {**voice.fields, "text": message.text, "session_id": message.session_id,
                  "project": message.project}
        self._send(self._queue, message, fields, origin)
        return True

    def _active_voice(self, message: Message) -> ActiveVoice | None:
        """Текущий голос для озвучки message; сбой чтения — None и строка в журнал: у
        озвученного кэш считается актуальным, отложенное остаётся без звука."""
        try:
            return self._daemon_settings.active_voice()
        except SettingsError as error:
            if message.deferred:
                log.warning("голос не прочитан (%s): отложенное сообщение %s не озвучено;"
                            " автопереход пропустит его как прослушанное, play"
                            " пользователя повторит", error, message.session_id)
            else:
                log.warning("голос не прочитан (%s), кэш считается актуальным", error)
            return None

    def _play_at(self, position: float) -> None:
        current = self._current
        if not current.sentences:
            # звука ещё нет: ждём предложение, как у растущего; не ждём — конец сообщения
            self._file = None
            self._sentence = None
            self._position = 0.0
            self._message_end()
            return
        path, offset = self._queue.locate(current, position)
        self._sentence = next(s for s in current.sentences if s.path == path)
        self._start_file(path, position - offset, position)

    def _play_sentence(self, sentence: Sentence) -> None:
        self._sentence = sentence
        base = self._queue.offset_of(self._current, sentence)
        self._start_file(sentence.path, base, base)

    def _start_file(self, path: Path, base: float, position: float) -> None:
        """Файл и позиция запомнены; звук пускает _follow_status (ворота звука)."""
        self._file = path
        self._file_base = base
        self._position = position
        self._due = True

    def _sound(self) -> None:
        self._due = False
        log.info("playing %s", self._file.name)
        self._playback.play(self._file, start_s=self._position - self._file_base)

    def _halt(self) -> None:
        self._due = False
        self._playback.stop()
        if self._retry_id:
            GLib.source_remove(self._retry_id)
            self._retry_id = 0
        self._file = None

    def _end_if_waiting(self) -> None:
        """Текущее ждёт предложение, а следующего нет: признак завершённости мог смениться без
        его предложения (предложение другой задачи, число предложений) — конец, если
        завершено."""
        if self._waiting() and self._next_sentence() is None:
            self._message_end()

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
        """Пауза других по статусу и ворота звука: начал звучать — снять таймер продолжения
        или поставить других на паузу; замолчал — продолжение через RESUME_DELAY_SECONDS.
        Запомненный файл звучит, когда ответа паузы не ждут и PAUSE_GAP_SECONDS не идут. Флажок
        pause_others снят — сначала другие отпущены (_release_others)."""
        if not self._pause_others:
            self._release_others()
        speaking = self._speaking()
        if speaking != self._spoke:
            self._spoke = speaking
            if speaking:
                self._speech_started()
            else:
                self._resume_later()
        if self._due and not self._pausing and not self._gap_id:
            self._sound()

    def _speaking(self) -> bool:
        """Для других плееров плеер звучит: играет сам или держит паузу ради своего текста."""
        return self._status == "playing" or self._held

    def _speech_started(self) -> None:
        """Таймер продолжения идёт — другие ещё стоят: снять его. Ответа ждут — паузу не
        повторять. Иначе — pause_playing, звук ждёт ответа; флажок снят — другие не трогаются."""
        if self._resume_id:
            self._cancel_resume()
        elif self._pause is not None and self._pause_others and not self._pausing:
            self._pausing = True
            self._pause.pause_playing(self._on_others_paused)

    def _on_others_paused(self, paused: bool) -> None:
        """Ответ pause_playing. Плеер звучит сам и кто-то встал — PAUSE_GAP_SECONDS тишины
        (в удержании и в idle звука этого плеера нет — тишину выдерживать некому); прежние 3 с
        ещё идут (пауза пользователя и снова ▶, пока другой плеер уже не держится) — звук по
        ним, второго отсчёта нет. Замолчал, пока ждал ответа, — продолжение через
        RESUME_DELAY_SECONDS. Флажок снят, пока ждали, — тишины нет, другие отпускает
        _follow_status из _changed."""
        self._pausing = False
        if not self._speaking():
            self._resume_later()
        elif paused and self._pause_others and self._status == "playing" and not self._gap_id:
            self._gap_id = GLib.timeout_add_seconds(self.PAUSE_GAP_SECONDS, self._gap_over)
        self._changed()

    def _gap_over(self) -> bool:
        self._gap_id = 0
        self._changed()
        return GLib.SOURCE_REMOVE

    def _resume_later(self) -> None:
        """Таймер продолжения, если пауза кого-то держит; ответа ждут — решит ответ; флажок
        снят — другие уже отпущены (_release_others)."""
        if (self._pause is None or not self._pause_others or self._pausing
                or not self._pause.holding):
            return
        self._resume_id = GLib.timeout_add_seconds(self.RESUME_DELAY_SECONDS, self._resume_others)

    def _resume_others(self) -> bool:
        self._resume_id = 0
        self._pause.resume()
        return GLib.SOURCE_REMOVE

    def _release_others(self) -> None:
        """Флажок pause_others снят: тишина 3 с и таймер продолжения сняты, пауза других
        отпущена сразу. Ответа pause_playing ждут — отпустит _follow_status после ответа
        (другие появляются в pause.holding с ответом). Повторный вызов ничего не делает."""
        if self._gap_id:
            GLib.source_remove(self._gap_id)
            self._gap_id = 0
        if self._resume_id:
            self._cancel_resume()
        if self._pause is not None and not self._pausing and self._pause.holding:
            self._pause.resume()

    def _cancel_resume(self) -> None:
        GLib.source_remove(self._resume_id)
        self._resume_id = 0

    def _focused(self, queue: MessageQueue) -> bool:
        """Фокус очереди — только у играемой (активной сессии) при нажатой кнопке
        автовоспроизведения; у закрытых папок его нет никогда, у открытой с отжатой кнопкой —
        тоже: она режется до tail, как закрытая (текущее trim не удаляет). Фокус: текущее есть
        и статус playing, paused или stopped; либо в idle есть ответ, до которого дойдёт
        автопереход, — новее текущего (без текущего — любой), отданный демону в этом запуске
        и ещё синтезируемый (не отложен, не прослушан, не завершён): его первое предложение начнёт его само, и пачка ответов быстрее синтеза не
        теряет ранние. Восстановленное из messages.json фокуса не даёт, даже растущее: иначе
        сообщение, чей конец прошёл без плеера и уже не будет замечен, держало бы очередь
        необрезанной."""
        if queue is not self._queue or not self._autoplay:
            return False
        if self._current is not None and self._status != "idle":
            return True
        ahead = queue.messages
        if self._current is not None:
            ahead = ahead[ahead.index(self._current) + 1:]
        return any(m in self._sent_in_run and m.sent and not m.deferred and not m.heard
                   and not queue.is_complete(m) for m in ahead)

    def _trim_unfocused(self) -> None:
        """Очереди без фокуса — до tail, самые старые первыми; текущее места в tail не
        занимает и не удаляется. Ушедшие из очередей забываются в _sent_in_run."""
        for queue in self._queues.values():
            if not self._focused(queue):
                queue.trim(keep=self._current)
        live = {m for queue in self._queues.values() for m in queue.messages}
        self._sent_in_run &= live

    def _changed(self) -> None:
        """Каждая смена статуса доходит сюда: сначала длина очередей по фокусу, пауза других
        и ворота звука по статусу, затем окно."""
        self._trim_unfocused()
        self._follow_status()
        try:
            self.on_change(self.state())
        except Exception:
            log.exception("on_change: ошибка обработчика окна")
