"""Воспроизведение одного WAV через GStreamer playbin; конец и ошибка — колбэками."""
import logging
from pathlib import Path
from typing import Callable

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

log = logging.getLogger("tts_player.playback")

Gst.init(None)


class Playback:
    """Играет один файл; конец — on_finished(path), сбой — on_error(path, text, output_failed).

    on_started(path) — конвейер дошёл до PLAYING (после seek, если он был): вывод звука
    открыт и звук пошёл. Sink, который не открывается, до PLAYING не доходит.

    output_failed=True — Gst.ResourceError от sink: недоступен вывод звука, файл ни при чём.
    Остальное (файл не найден, не декодируется) — ошибка файла, output_failed=False.
    """

    def __init__(self, audio_sink: str | None = None) -> None:
        self.on_finished: Callable[[Path], None] = lambda path: None
        self.on_error: Callable[[Path, str, bool], None] = lambda path, text, output_failed: None
        self.on_started: Callable[[Path], None] = lambda path: None
        self._audio_sink = audio_sink
        self._player: Gst.Element | None = None
        self._bus: Gst.Bus | None = None
        self._handler_id = 0
        self._seek_ns: int | None = None
        self._seek_issued = False
        if audio_sink is not None:
            self._make_sink()

    def play(self, path: Path, start_s: float = 0.0) -> None:
        self.stop()
        player = Gst.ElementFactory.make("playbin", None)
        if self._audio_sink is not None:
            player.set_property("audio-sink", self._make_sink())
        player.set_property("uri", Gst.filename_to_uri(str(path)))
        bus = player.get_bus()
        bus.add_signal_watch()
        self._handler_id = bus.connect("message", self._on_message, path)
        self._player = player
        self._bus = bus
        if start_s > 0:
            self._seek_ns = int(start_s * Gst.SECOND)
            self._seek_issued = False
            player.set_state(Gst.State.PAUSED)
        else:
            self._seek_ns = None
            player.set_state(Gst.State.PLAYING)

    def position(self) -> float | None:
        """Секунды от начала файла; None — ничего не играет или позиция недоступна."""
        player = self._player
        if player is None:
            return None
        if self._seek_ns is not None:
            return self._seek_ns / Gst.SECOND
        ok, position_ns = player.query_position(Gst.Format.TIME)
        if not ok:
            return None
        return position_ns / Gst.SECOND

    def stop(self) -> None:
        player, bus = self._player, self._bus
        self._player = None
        self._bus = None
        self._seek_ns = None
        if player is None:
            return
        bus.disconnect(self._handler_id)
        bus.remove_signal_watch()
        player.set_state(Gst.State.NULL)

    def _make_sink(self) -> Gst.Element:
        sink = Gst.ElementFactory.make(self._audio_sink, None)
        if sink is None:
            raise ValueError(f"unknown GStreamer element: {self._audio_sink}")
        if self._audio_sink == "fakesink":
            sink.set_property("sync", True)
        return sink

    def _on_message(self, bus: Gst.Bus, message: Gst.Message, path: Path) -> None:
        if message.type == Gst.MessageType.ASYNC_DONE:
            self._continue_start(path)
        elif message.type == Gst.MessageType.STATE_CHANGED:
            if message.src == self._player:
                _, new, _ = message.parse_state_changed()
                if new == Gst.State.PLAYING:
                    self.on_started(path)
        elif message.type == Gst.MessageType.EOS:
            self.stop()
            self.on_finished(path)
        elif message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            log.debug("playback of %s failed: %s (%s)", path, error.message, debug)
            output_failed = self._is_output_error(message.src, error)
            self.stop()
            self.on_error(path, error.message, output_failed)

    def _continue_start(self, path: Path) -> None:
        """PAUSED → ASYNC_DONE → seek → ASYNC_DONE → PLAYING; сбой seek — файл доигран."""
        if self._seek_ns is None or self._player is None:
            return
        player = self._player
        if self._seek_issued:
            self._seek_ns = None
            player.set_state(Gst.State.PLAYING)
            return
        self._seek_issued = True
        flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE
        if not player.seek_simple(Gst.Format.TIME, flags, self._seek_ns):
            self.stop()
            self.on_finished(path)

    @staticmethod
    def _is_output_error(source: Gst.Object, error: GLib.Error) -> bool:
        if not error.matches(Gst.ResourceError.quark(), error.code):
            return False
        return bool(source.flags & Gst.ElementFlags.SINK)
