"""Счётчик предложений сообщения: процесс sentence_count_worker в окружении claude-tts делит
текст той же функцией, что и демон; ответ приходит в цикл GLib."""
import collections
import json
import logging
from pathlib import Path
from typing import Callable

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

log = logging.getLogger("tts_player.sentence_counter")


class SentenceCounter:
    """Постоянный процесс-счётчик: count(text, on_count) — запрос, on_count(n) — ответ из цикла
    GLib, по порядку запросов. Процесс запускается первым запросом и живёт до stop(); его
    владелец — Player (останавливает в shutdown).

    Главный цикл не ждёт: запрос пишется в stdin асинхронно, ответ читается асинхронно. Сбой —
    процесс не запустился, вышел, прислал непонятное или не ответил на самый старый запрос за
    TIMEOUT_S — одна строка WARNING, процесс остановлен, счётчик недоступен до конца запуска:
    ждущие и новые запросы ответа не получают (число у сообщения остаётся неизвестным, конец
    сообщения — по запасным признакам MessageQueue.is_complete).
    """
    # ответ на текст сообщения — доли миллисекунды, запуск процесса с импортом claude_code_tts
    # — десятки; 5 с — заведомо сбой, а не медленный счёт
    TIMEOUT_S = 5.0

    def __init__(self, python: Path, worker: Path) -> None:
        self._argv = [str(python), "-P", str(worker)]  # -P: каталог воркера не в sys.path
        self._process: Gio.Subprocess | None = None
        self._cancellable = Gio.Cancellable()
        self._stdin: Gio.OutputStream | None = None
        self._stdout: Gio.DataInputStream | None = None
        self._outbox: collections.deque[bytes] = collections.deque()
        self._writing = False
        self._pending: collections.deque[Callable[[int], None]] = collections.deque()
        self._timeout_id = 0
        self._broken = False  # сбой или stop: запросы больше не принимаются

    def count(self, text: str, on_count: Callable[[int], None]) -> None:
        """Запрос числа предложений text; ответ — в on_count из цикла GLib. Счётчик
        недоступен — ответа не будет."""
        if self._broken:
            return
        if self._process is None and not self._launch():
            return
        self._pending.append(on_count)
        # ASCII с \uXXXX: одиночный суррогат (json.loads задачи хука его пропускает) в UTF-8 не
        # кодируется, а экранированным процесс получает ту же строку, что и демон
        self._outbox.append(json.dumps({"text": text}).encode("ascii") + b"\n")
        self._write_next()
        self._arm_timeout()

    def stop(self) -> None:
        """Выход плеера: процесс убит и подобран, ждущие запросы без ответа."""
        self._broken = True
        self._end()

    def _launch(self) -> bool:
        flags = Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE
        try:
            self._process = Gio.Subprocess.new(self._argv, flags)
        except GLib.Error as error:
            self._fail(f"процесс не запустился: {error.message}")
            return False
        self._stdin = self._process.get_stdin_pipe()
        self._stdout = Gio.DataInputStream.new(self._process.get_stdout_pipe())
        self._read_next()
        return True

    def _write_next(self) -> None:
        """Одна запись за раз: у потока Gio не бывает двух асинхронных операций сразу."""
        if self._writing or not self._outbox:
            return
        self._writing = True
        self._stdin.write_all_async(self._outbox.popleft(), GLib.PRIORITY_DEFAULT,
                                    self._cancellable, self._written)

    def _written(self, stream: Gio.OutputStream, result: Gio.AsyncResult) -> None:
        try:
            stream.write_all_finish(result)
        except GLib.Error as error:
            if not self._cancellable.is_cancelled():
                self._fail(f"запрос не записан: {error.message}")
            return
        self._writing = False
        self._write_next()

    def _read_next(self) -> None:
        self._stdout.read_line_async(GLib.PRIORITY_DEFAULT, self._cancellable, self._read)

    def _read(self, stream: Gio.DataInputStream, result: Gio.AsyncResult) -> None:
        try:
            line, _ = stream.read_line_finish_utf8(result)
        except GLib.Error as error:
            if not self._cancellable.is_cancelled():
                self._fail(f"вывод не прочитан: {error.message}")
            return
        if line is None:
            self._fail("процесс завершился")
            return
        count = self._parse(line)
        if isinstance(count, str) or not self._pending:
            self._fail(count if isinstance(count, str) else f"ответ без запроса {line!r}")
            return
        self._disarm_timeout()
        on_count = self._pending.popleft()
        self._arm_timeout()
        self._read_next()
        try:
            on_count(count)
        except Exception:
            log.exception("on_count: ошибка обработчика числа предложений")

    @staticmethod
    def _parse(line: str) -> int | str:
        """{"count": n} с целым n ≥ 0 — n; иначе причина сбоя для WARNING."""
        try:
            message = json.loads(line)
        except ValueError:
            message = None
        if not isinstance(message, dict):
            return f"непонятная строка {line!r}"
        if "error" in message:
            return f"ошибка процесса: {message['error']}"
        count = message.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return f"непонятная строка {line!r}"
        return count

    def _arm_timeout(self) -> None:
        """Срок TIMEOUT_S — на самый старый запрос без ответа, от момента постановки срока:
        пока идёт, новый запрос его не сдвигает; ответ снимает срок, и следующий ждущий
        запрос получает новый полный TIMEOUT_S. Счётчик, ответивший хоть на что-то, жив."""
        if self._pending and not self._timeout_id:
            self._timeout_id = GLib.timeout_add(int(self.TIMEOUT_S * 1000), self._expired)

    def _disarm_timeout(self) -> None:
        if self._timeout_id:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = 0

    def _expired(self) -> bool:
        self._timeout_id = 0
        self._fail(f"нет ответа за {self.TIMEOUT_S:g} с")
        return GLib.SOURCE_REMOVE

    def _fail(self, reason: str) -> None:
        """Одна строка WARNING на запуск плеера; процесс остановлен, счётчик недоступен."""
        if self._broken:
            return
        self._broken = True
        log.warning("счётчик предложений недоступен (%s): конец сообщения — по"
                    " предложению следующей задачи", reason)
        self._end()

    def _end(self) -> None:
        """Колбэки сняты, срок снят; живой процесс убит SIGKILL и подобран здесь же: у счётчика
        нет состояния, которое стоило бы сохранять, и детей нет."""
        self._cancellable.cancel()
        self._disarm_timeout()
        self._pending.clear()
        self._outbox.clear()
        process, self._process = self._process, None
        if process is None or process.get_identifier() is None:
            return
        process.force_exit()
        process.wait(None)
