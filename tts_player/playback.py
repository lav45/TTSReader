"""Воспроизведение одного WAV через GStreamer playbin; конец и ошибка — колбэками."""
import logging
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

log = logging.getLogger("tts_player.playback")

Gst.init(None)


class Playback:
    def __init__(self, audio_sink: str | None = None) -> None:
        self.on_finished: Callable[[Path], None] = lambda path: None
        self.on_error: Callable[[Path, str], None] = lambda path, text: None
        self._audio_sink = audio_sink
        self._player: Gst.Element | None = None
        self._bus: Gst.Bus | None = None
        self._handler_id = 0
        if audio_sink is not None:
            self._make_sink()

    def play(self, path: Path) -> None:
        self.stop()
        player = Gst.ElementFactory.make("playbin", None)
        if self._audio_sink is not None:
            player.set_property("audio-sink", self._make_sink())
        player.set_property("uri", Gst.filename_to_uri(str(path)))
        bus = player.get_bus()
        bus.add_signal_watch()
        self._handler_id = bus.connect("message", self._on_message, player, path)
        self._player = player
        self._bus = bus
        player.set_state(Gst.State.PLAYING)

    def stop(self) -> None:
        player, bus = self._player, self._bus
        self._player = None
        self._bus = None
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

    def _on_message(self, bus: Gst.Bus, message: Gst.Message, player: Gst.Element, path: Path) -> None:
        if player is not self._player:
            return
        if message.type == Gst.MessageType.EOS:
            self.stop()
            self.on_finished(path)
        elif message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            log.error("playback of %s failed: %s (%s)", path, error.message, debug)
            self.stop()
            self.on_error(path, error.message)
