"""Воспроизведение одного WAV через GStreamer playbin; конец и ошибка — колбэками."""
import logging
from pathlib import Path
from typing import Callable

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from tts_player.output_idle import OutputIdle  # noqa: E402

log = logging.getLogger("tts_player.playback")

Gst.init(None)

WAKE_SILENCE_S = 0.5  # проснувшийся Bluetooth-sink теряет ~0,3 с звука
SILENCE_RATE = 48000


class Playback:
    """Играет один файл; конец — on_finished(path), сбой — on_error(path, text, output_failed).

    on_started(path) — конвейер дошёл до PLAYING (после seek, если он был): вывод звука
    открыт и звук пошёл; один раз на play(). Sink, который не открывается, до PLAYING не
    доходит.

    output_failed=True — Gst.ResourceError от sink: недоступен вывод звука, файл ни при чём.
    Остальное (файл не найден, не декодируется) — ошибка файла, output_failed=False.

    output — простой вывода, общий для всех Playback процесса; None — без тишины. Вывод
    спит (output.asleep()) — перед файлом WAKE_SILENCE_S тишины тем же sink: вывод
    просыпается на ней, а не на начале речи. Файл и позиция в нём от этого не меняются:
    во время тишины position() — start_s, on_started — когда звучит тишина, сбой вывода на
    тишине — on_error(path, …, True).
    """

    def __init__(self, audio_sink: str | None = None, output: OutputIdle | None = None) -> None:
        self.on_finished: Callable[[Path], None] = lambda path: None
        self.on_error: Callable[[Path, str, bool], None] = lambda path, text, output_failed: None
        self.on_started: Callable[[Path], None] = lambda path: None
        self._audio_sink = audio_sink
        self._output = output
        self._player: Gst.Element | None = None
        self._bus: Gst.Bus | None = None
        self._handler_id = 0
        self._seek_ns: int | None = None
        self._seek_issued = False
        self._waiting: tuple[Path, float] | None = None  # файл после тишины: путь, start_s
        self._sounding = False  # output.begin() вызван для текущего конвейера
        self._started = False  # on_started уже вызван для этого play()
        if audio_sink is not None:
            self._make_sink()

    def play(self, path: Path, start_s: float = 0.0) -> None:
        self.stop()
        self._started = False
        if self._output is not None and self._output.asleep():
            self._play_silence(path, start_s)
            return
        self._play_file(path, start_s)

    def position(self) -> float | None:
        """Секунды от начала файла; None — ничего не играет или позиция недоступна."""
        player = self._player
        if player is None:
            return None
        if self._waiting is not None:
            return self._waiting[1]
        if self._seek_ns is not None:
            return self._seek_ns / Gst.SECOND
        ok, position_ns = player.query_position(Gst.Format.TIME)
        if not ok:
            return None
        return position_ns / Gst.SECOND

    def stop(self) -> None:
        self._waiting = None
        self._teardown()

    def _play_file(self, path: Path, start_s: float) -> None:
        player = Gst.ElementFactory.make("playbin", None)
        if self._audio_sink is not None:
            player.set_property("audio-sink", self._make_sink())
        player.set_property("uri", Gst.filename_to_uri(str(path)))
        self._attach(player, self._on_message, path)
        if start_s > 0:
            self._seek_ns = int(start_s * Gst.SECOND)
            self._seek_issued = False
            player.set_state(Gst.State.PAUSED)
            return
        self._seek_ns = None
        player.set_state(Gst.State.PLAYING)

    def _play_silence(self, path: Path, start_s: float) -> None:
        """WAKE_SILENCE_S тишины одним буфером; её конец (EOS) запускает файл."""
        pipeline = Gst.Pipeline.new(None)
        source = Gst.ElementFactory.make("audiotestsrc", None)
        source.set_property("wave", "silence")
        source.set_property("num-buffers", 1)
        source.set_property("samplesperbuffer", int(WAKE_SILENCE_S * SILENCE_RATE))
        caps = Gst.ElementFactory.make("capsfilter", None)
        caps.set_property("caps", Gst.Caps.from_string(f"audio/x-raw,rate={SILENCE_RATE}"))
        convert = Gst.ElementFactory.make("audioconvert", None)
        resample = Gst.ElementFactory.make("audioresample", None)
        sink = self._make_sink() if self._audio_sink is not None else (
            Gst.ElementFactory.make("autoaudiosink", None))
        elements = [source, caps, convert, resample, sink]
        for element in elements:
            pipeline.add(element)
        for upstream, downstream in zip(elements, elements[1:]):
            upstream.link(downstream)
        self._waiting = (path, start_s)
        self._attach(pipeline, self._on_silence_message, path)
        pipeline.set_state(Gst.State.PLAYING)

    def _attach(self, pipeline: Gst.Element, handler: Callable, path: Path) -> None:
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        self._handler_id = bus.connect("message", handler, path)
        self._player = pipeline
        self._bus = bus

    def _teardown(self) -> None:
        """Текущий конвейер (файл или тишина) — в NULL; его звук для output кончился."""
        player, bus = self._player, self._bus
        self._player = None
        self._bus = None
        self._seek_ns = None
        if self._sounding:
            self._sounding = False
            self._output.end()
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

    def _began(self, path: Path) -> None:
        """Конвейер дошёл до PLAYING: звучит для output; on_started — первый раз за play()."""
        if self._output is not None and not self._sounding:
            self._sounding = True
            self._output.begin()
        if self._started:
            return
        self._started = True
        self.on_started(path)

    def _on_message(self, bus: Gst.Bus, message: Gst.Message, path: Path) -> None:
        if message.type == Gst.MessageType.ASYNC_DONE:
            self._continue_start(path)
        elif message.type == Gst.MessageType.STATE_CHANGED:
            if message.src == self._player:
                _, new, _ = message.parse_state_changed()
                if new == Gst.State.PLAYING:
                    self._began(path)
        elif message.type == Gst.MessageType.EOS:
            self.stop()
            self.on_finished(path)
        elif message.type == Gst.MessageType.ERROR:
            self._fail(message, path)

    def _on_silence_message(self, bus: Gst.Bus, message: Gst.Message, path: Path) -> None:
        if message.type == Gst.MessageType.STATE_CHANGED:
            if message.src == self._player:
                _, new, _ = message.parse_state_changed()
                if new == Gst.State.PLAYING:
                    self._began(path)
        elif message.type == Gst.MessageType.EOS:
            path, start_s = self._waiting
            self._waiting = None
            self._teardown()
            self._play_file(path, start_s)
        elif message.type == Gst.MessageType.ERROR:
            self._fail(message, path)

    def _fail(self, message: Gst.Message, path: Path) -> None:
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
