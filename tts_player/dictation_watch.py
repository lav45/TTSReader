"""Слежение за диктовкой Claude Code: процесс `pactl subscribe` сообщает о потоках записи
PipeWire, разовый `pactl -f json list` показывает, чей поток; ответ приходит в цикл GLib."""
import json
import logging
import os
import re
from pathlib import Path
from typing import Callable

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

log = logging.getLogger("tts_player.dictation_watch")

# появление и исчезновение потока записи; change (громкость, пауза) состава потоков не меняет
_STREAM_EVENT = re.compile(r"Event '(new|remove)' on source-output #\d+")


class DictationWatch:
    """Диктовка идёт ⇔ есть поток записи (source-output), у клиента которого процесс
    (pipewire.sec.pid ставит сам PipeWire) запущен из claude_dir. Имя потока не смотрится.

    on_change(active) — только при смене, из цикла GLib. start() запускает `pactl subscribe` и
    первую проверку; на new/remove по source-output — проверка списком потоков и клиентов.
    Проверки не перекрываются: событие во время проверки — ещё одна проверка после неё (сколько
    бы событий ни пришло). Владелец останавливает stop().

    Каждый вызов pactl — с LC_ALL=C: в русской локали pactl портит JSON. Сбой — pactl не
    запустился, subscribe вышел, список не прочитан — одна строка WARNING, диктовка считается
    неидущей (on_change(False), если шла), слежение до конца запуска не возобновляется.
    """

    def __init__(self, on_change: Callable[[bool], None], *,
                 pactl: str = "pactl",
                 claude_dir: Path = Path.home() / ".local/share/claude/versions") -> None:
        self._on_change = on_change
        self._pactl = pactl
        self._claude_dir = claude_dir
        self._cancellable = Gio.Cancellable()
        self._subscribe: Gio.Subprocess | None = None
        self._stdout: Gio.DataInputStream | None = None
        self._list: Gio.Subprocess | None = None  # идущий `pactl list`
        self._checking = False
        self._recheck = False  # событие пришло во время проверки
        self._active = False
        self._broken = False  # сбой или stop: событий и колбэков больше нет

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> None:
        """Процесс subscribe и первая проверка: диктовка могла идти до запуска плеера."""
        try:
            # stderr не перехватывается: subscribe живёт долго, его жалобы идут в журнал плеера
            self._subscribe = self._spawn(Gio.SubprocessFlags.STDOUT_PIPE, "subscribe")
        except GLib.Error as error:
            self._fail(f"pactl subscribe не запустился: {error.message}")
            return
        self._stdout = Gio.DataInputStream.new(self._subscribe.get_stdout_pipe())
        self._read_next()
        self._check()

    def stop(self) -> None:
        """Выход плеера: процессы убиты и подобраны, колбэков больше нет."""
        self._broken = True
        self._end()

    def _spawn(self, flags: Gio.SubprocessFlags, *args: str) -> Gio.Subprocess:
        launcher = Gio.SubprocessLauncher.new(flags)
        launcher.setenv("LC_ALL", "C", True)
        return launcher.spawnv([self._pactl, *args])

    def _read_next(self) -> None:
        self._stdout.read_line_async(GLib.PRIORITY_DEFAULT, self._cancellable, self._read)

    def _read(self, stream: Gio.DataInputStream, result: Gio.AsyncResult) -> None:
        if self._broken:
            return
        try:
            line, _ = stream.read_line_finish_utf8(result)
        except GLib.Error as error:
            if not self._cancellable.is_cancelled():
                self._fail(f"вывод pactl subscribe не прочитан: {error.message}")
            return
        if line is None:
            self._fail("pactl subscribe завершился")
            return
        self._read_next()
        if _STREAM_EVENT.fullmatch(line):
            self._check()

    def _check(self) -> None:
        """Проверка: список потоков, затем (если потоки есть) список клиентов, затем решение.
        Идёт проверка — отметка «ещё раз», одна на сколько угодно событий."""
        if self._checking:
            self._recheck = True
            return
        self._checking = True
        self._list_json("source-outputs", self._outputs_listed)

    def _list_json(self, what: str, on_list: Callable[[list], None]) -> None:
        """`pactl -f json list <what>`; разобранный список — в on_list, иначе сбой."""
        try:
            # stderr list читает communicate_utf8: причина сбоя попадает в WARNING
            flags = Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
            process = self._spawn(flags, "-f", "json", "list", what)
        except GLib.Error as error:
            self._fail(f"pactl list {what} не запустился: {error.message}")
            return
        self._list = process

        def done(process: Gio.Subprocess, result: Gio.AsyncResult) -> None:
            if self._broken:
                return
            try:
                _, stdout, stderr = process.communicate_utf8_finish(result)
            except GLib.Error as error:
                if not self._cancellable.is_cancelled():
                    self._fail(f"pactl list {what} не прочитан: {error.message}")
                return
            self._list = None
            if not process.get_successful():
                self._fail(f"pactl list {what}: {(stderr or '').strip() or 'ошибка'}")
                return
            try:
                items = json.loads(stdout)
            except ValueError:
                items = None
            if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
                self._fail(f"pactl list {what}: непонятный ответ {stdout[:200]!r}")
                return
            on_list(items)

        process.communicate_utf8_async(None, self._cancellable, done)

    def _outputs_listed(self, outputs: list[dict]) -> None:
        clients = {str(output.get("client")) for output in outputs}
        if not clients:
            self._checked(False)
            return
        self._list_json("clients", lambda listed: self._checked(any(
            self._is_claude(client) for client in listed
            if str(client.get("index")) in clients)))

    def _is_claude(self, client: dict) -> bool:
        """Процесс клиента запущен из claude_dir. Нет pid или процесса — не Claude Code."""
        properties = client.get("properties")
        pid = properties.get("pipewire.sec.pid") if isinstance(properties, dict) else None
        if not isinstance(pid, str) or not pid.isdigit():
            return False
        try:
            exe = Path(os.readlink(f"/proc/{pid}/exe"))
        except OSError:
            return False
        # после обновления Claude Code у ещё работающей версии « (deleted)» в имени, а не в каталоге
        return exe.parent == self._claude_dir.resolve()

    def _checked(self, active: bool) -> None:
        self._checking = False
        if self._recheck:
            self._recheck = False
            self._check()
            return
        self._set_active(active)

    def _set_active(self, active: bool) -> None:
        if active == self._active:
            return
        self._active = active
        try:
            self._on_change(active)
        except Exception:
            log.exception("on_change: ошибка обработчика диктовки")

    def _fail(self, reason: str) -> None:
        """Одна строка WARNING на запуск плеера; процессы остановлены, диктовка не идёт."""
        if self._broken:
            return
        self._broken = True
        log.warning("слежение за диктовкой недоступно (%s): озвучка диктовку не ждёт", reason)
        self._end()
        self._set_active(False)

    def _end(self) -> None:
        """Колбэки чтения сняты; живые процессы pactl убиты SIGKILL и подобраны здесь же."""
        self._cancellable.cancel()
        self._checking = False
        self._recheck = False
        for process in (self._subscribe, self._list):
            if process is not None and process.get_identifier() is not None:
                process.force_exit()
                process.wait(None)
        self._subscribe = None
        self._stdout = None
        self._list = None
