"""Пауза чужих медиаплееров через D-Bus MPRIS на время озвучки.

MprisPause ставит на паузу играющих плееров, запоминает прошедших и продолжает тех, кого
пользователь за время речи не тронул. Все вызовы D-Bus асинхронные, с таймаутом; ошибки
(нет ответа, ошибка D-Bus) — предупреждение в лог, наружу исключение не выходит."""
import logging
from collections.abc import Callable

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

log = logging.getLogger(__name__)

CALL_TIMEOUT_MS = 1000
MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
PLAYER_INTERFACE = "org.mpris.MediaPlayer2.Player"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
DBUS_NAME = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
PLAYING = "Playing"
PAUSED = "Paused"


class MprisPause:
    """Ставит на паузу играющих MPRIS-плееров и продолжает тех, кто остался на паузе."""

    def __init__(self, connection: Gio.DBusConnection) -> None:
        self._connection = connection
        self._held: dict[str, list[int]] = {}  # имя плеера -> подписки на его сигналы
        self._pausing: set[str] = set()  # Pause() отправлен, ответа ещё нет
        self._play_on_paused: set[str] = set()  # resume() был до ответа на Pause(): ответ → Play()
        self._stopped = False

    @property
    def holding(self) -> bool:
        """Есть запомненные плееры, которых пауза ещё держит."""
        return bool(self._held)

    def pause_playing(self, done: Callable[[bool], None]) -> None:
        """Каждому играющему — Pause(); прошедших запомнить.

        done(True) — хоть один Pause() прошёл, done(False) — ни одного; вызывается один раз,
        после ответов всех (или их таймаутов и ошибок)."""
        self._call(DBUS_NAME, DBUS_PATH, DBUS_NAME, "ListNames", None,
                   lambda reply: self._on_names(reply, done))

    def resume(self) -> None:
        """Каждому запомненному — Play(); запомненные забыты.

        Список и так держит только стоящих на паузе: ставший Playing / Stopped или ушедший с шины
        забыт по сигналу, поэтому статус перед Play() не спрашивается. Тому, чей Pause() ещё не
        ответил, Play() уйдёт с ответом, а если раньше наступит stop — в stop, не дожидаясь
        ответа."""
        for name in list(self._held):
            self._forget(name)
            if name in self._pausing:
                self._play_on_paused.add(name)
                continue
            self._play(name)

    def stop(self) -> None:
        """Отписаться от сигналов; после stop колбэков нет. Уже отправленные Play() не отменяются:
        соединение сбрасывается, чтобы они ушли до выхода процесса.

        Play(), ждущий ответа на Pause() (resume до ответа), уходит здесь же: после stop главный
        цикл при выходе уже не крутится, и ответа процесс не дождётся. Сообщения одного
        соединения одному адресату доставляются по порядку — Play() придёт после Pause();
        ответ, если всё же придёт, второго Play() не шлёт."""
        self._stopped = True
        for name in list(self._held):
            self._forget(name)
        for name in self._play_on_paused:
            self._play(name)
        self._play_on_paused.clear()
        try:
            self._connection.flush_sync(None)
        except GLib.Error as exc:
            log.warning("MPRIS: соединение не сброшено: %s", exc.message)

    def _on_names(self, reply: GLib.Variant | None, done: Callable[[bool], None]) -> None:
        names = [] if reply is None else [n for n in reply.unpack()[0] if n.startswith(MPRIS_PREFIX)]
        if not names:
            done(False)
            return
        state = {"pending": len(names), "paused": False}

        def finish(paused: bool) -> None:
            state["paused"] = state["paused"] or paused
            state["pending"] -= 1
            if state["pending"] == 0 and not self._stopped:
                done(state["paused"])

        for name in names:
            self._get_status(name, lambda status, name=name: self._pause_if_playing(name, status, finish))

    def _pause_if_playing(self, name: str, status: str | None, finish: Callable[[bool], None]) -> None:
        if status != PLAYING or name in self._held or name in self._pausing:
            finish(False)
            return
        self._remember(name)
        self._pausing.add(name)
        self._call(name, MPRIS_PATH, PLAYER_INTERFACE, "Pause", None,
                   lambda reply: self._on_paused(name, reply, finish), survive_stop=True)

    def _on_paused(self, name: str, reply: GLib.Variant | None, finish: Callable[[bool], None]) -> None:
        self._pausing.discard(name)
        if reply is None:
            self._forget(name)
        if name in self._play_on_paused:
            self._play_on_paused.discard(name)
            if reply is not None:
                self._play(name)
        if not self._stopped:
            finish(reply is not None)

    def _play(self, name: str) -> None:
        self._call(name, MPRIS_PATH, PLAYER_INTERFACE, "Play", None, None, survive_stop=True)

    def _get_status(self, name: str, callback: Callable[[str | None], None]) -> None:
        """PlaybackStatus плеера; None — ответа нет или ошибка."""
        params = GLib.Variant("(ss)", (PLAYER_INTERFACE, "PlaybackStatus"))
        self._call(name, MPRIS_PATH, PROPERTIES_INTERFACE, "Get", params,
                   lambda reply: callback(None if reply is None else reply.unpack()[0]))

    def _call(self, name: str, path: str, interface: str, method: str, params: GLib.Variant | None,
              callback: Callable[[GLib.Variant | None], None] | None,
              survive_stop: bool = False) -> None:
        """Асинхронный вызов с таймаутом; callback получает ответ или None (ошибка уже в логе).

        После stop callback не зовётся, если не survive_stop (ответ, от которого зависит Play())."""
        def finished(connection: Gio.DBusConnection, result: Gio.AsyncResult) -> None:
            try:
                reply = connection.call_finish(result)
            except GLib.Error as exc:
                log.warning("MPRIS: вызов %s к %s не удался: %s", method, name, exc.message)
                reply = None
            if callback is not None and (survive_stop or not self._stopped):
                callback(reply)

        self._connection.call(name, path, interface, method, params, None,
                              Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, None, finished)

    def _remember(self, name: str) -> None:
        """Запомнить плеера и следить: сменил статус не на Paused или ушёл с шины — забыть."""
        changed = self._connection.signal_subscribe(
            name, PROPERTIES_INTERFACE, "PropertiesChanged", MPRIS_PATH, None,
            Gio.DBusSignalFlags.NONE, self._on_properties_changed, name)
        owner = self._connection.signal_subscribe(
            DBUS_NAME, DBUS_NAME, "NameOwnerChanged", DBUS_PATH, name,
            Gio.DBusSignalFlags.NONE, self._on_owner_changed, name)
        self._held[name] = [changed, owner]

    def _forget(self, name: str) -> None:
        for subscription in self._held.pop(name, []):
            self._connection.signal_unsubscribe(subscription)

    def _on_properties_changed(self, connection, sender, path, interface, signal, params, name) -> None:
        _, changed, _ = params.unpack()
        status = changed.get("PlaybackStatus")
        if status is not None and status != PAUSED:
            self._forget(name)

    def _on_owner_changed(self, connection, sender, path, interface, signal, params, name) -> None:
        _, _, new_owner = params.unpack()
        if not new_owner:
            self._forget(name)
