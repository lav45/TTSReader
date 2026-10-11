"""Поддельные MPRIS-плееры на временной шине Gio.TestDBus (общие для тестов MprisPause и Player).

FakeBus поднимает свою шину на каждый тест; FakePlayer занимает имя org.mpris.MediaPlayer2.<имя>,
отвечает на Get(PlaybackStatus), Pause, Play, шлёт PropertiesChanged при смене статуса и пишет
полученные вызовы в журнал calls."""
import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
PLAYER_INTERFACE = "org.mpris.MediaPlayer2.Player"
PLAYER_XML = """<node><interface name="org.mpris.MediaPlayer2.Player">
<method name="Pause"/><method name="Play"/>
<property name="PlaybackStatus" type="s" access="read"/></interface></node>"""

ANSWER = "answer"
ERROR = "error"
SILENT = "silent"


class FakeBus:
    """Временная сессионная шина; connect() даёт новое соединение с ней."""

    def __init__(self) -> None:
        self._bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
        self._bus.up()
        self._connections: list[Gio.DBusConnection] = []

    def connect(self) -> Gio.DBusConnection:
        flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
        connection = Gio.DBusConnection.new_for_address_sync(self._bus.get_bus_address(), flags, None, None)
        self._connections.append(connection)
        return connection

    def down(self) -> None:
        for connection in self._connections:
            if not connection.is_closed():
                connection.close_sync(None)
        self._bus.down()


class FakePlayer:
    """MPRIS-плеер: mode — ANSWER (ответ), ERROR (ошибка D-Bus) или SILENT (на Pause не отвечает)."""

    def __init__(self, bus: FakeBus, name: str, status: str, mode: str = ANSWER) -> None:
        self.name = MPRIS_PREFIX + name
        self.status = status
        self.mode = mode
        self.calls: list[str] = []
        self._silent: list[Gio.DBusMethodInvocation] = []
        self._connection = bus.connect()
        node = Gio.DBusNodeInfo.new_for_xml(PLAYER_XML)
        self._connection.register_object(MPRIS_PATH, node.interfaces[0], self._on_call, self._on_get, None)
        self._connection.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
            GLib.Variant("(su)", (self.name, 0)), None, Gio.DBusCallFlags.NONE, -1, None)

    def set_status(self, status: str) -> None:
        """Сменить статус «снаружи» (как пользователь) и разослать PropertiesChanged."""
        self.status = status
        changed = {"PlaybackStatus": GLib.Variant("s", status)}
        self._connection.emit_signal(
            None, MPRIS_PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
            GLib.Variant("(sa{sv}as)", (PLAYER_INTERFACE, changed, [])))

    def vanish(self) -> None:
        """Имя уходит с шины: соединение плеера закрыто."""
        self._connection.close_sync(None)

    def _on_get(self, connection, sender, path, interface, prop) -> GLib.Variant:
        return GLib.Variant("s", self.status)

    def _on_call(self, connection, sender, path, interface, method, params, invocation) -> None:
        self.calls.append(method)
        if self.mode == SILENT:
            self._silent.append(invocation)  # ссылка держится, иначе GDBus ответит сам
            return
        if self.mode == ERROR:
            invocation.return_dbus_error("org.example.Failed", "не получилось")
            return
        self.set_status("Paused" if method == "Pause" else "Playing")
        invocation.return_value(None)
