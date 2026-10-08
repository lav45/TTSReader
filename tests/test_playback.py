import os
import tempfile
import time
import unittest
import wave
from pathlib import Path

from gi.repository import GLib

from tts_player.playback import Playback

RATE = 8000
TOLERANCE = 0.05
LOOP_LIMIT_MS = 5000


def make_wav(path: Path, seconds: float) -> Path:
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(b"\x00\x00" * int(RATE * seconds))
    return path


class PlaybackTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.loop = GLib.MainLoop()
        self.finished: list[Path] = []
        self.errors: list[tuple[Path, str]] = []
        self.playback = Playback("fakesink")
        self.addCleanup(self.playback.stop)
        self.playback.on_finished = self._finished
        self.playback.on_error = self._errored

    def _finished(self, path: Path) -> None:
        self.finished.append(path)
        self.loop.quit()

    def _errored(self, path: Path, text: str) -> None:
        self.errors.append((path, text))
        self.loop.quit()

    def run_loop(self, ms: int = LOOP_LIMIT_MS) -> None:
        GLib.timeout_add(ms, self.loop.quit)
        self.loop.run()

    def test_unknown_sink_raises(self) -> None:
        with self.assertRaises(ValueError):
            Playback("no-such-element-xyz")

    def test_finish_not_earlier_than_duration(self) -> None:
        duration = 0.3
        wav = make_wav(self.dir / "a.wav", duration)
        started = time.monotonic()
        self.playback.play(wav)
        self.run_loop()
        elapsed = time.monotonic() - started
        self.assertEqual([wav], self.finished)
        self.assertEqual([], self.errors)
        self.assertGreaterEqual(elapsed, duration - TOLERANCE)

    def test_finished_called_once(self) -> None:
        wav = make_wav(self.dir / "a.wav", 0.2)
        self.playback.play(wav)
        self.run_loop()
        self.run_loop(400)
        self.assertEqual([wav], self.finished)

    def test_garbage_reports_error(self) -> None:
        bad = self.dir / "bad.wav"
        bad.write_bytes(os.urandom(2048))
        self.playback.play(bad)
        self.run_loop()
        self.assertEqual([], self.finished)
        self.assertEqual(1, len(self.errors))
        self.assertEqual(bad, self.errors[0][0])
        self.assertTrue(self.errors[0][1])

    def test_play_interrupts_current(self) -> None:
        first = make_wav(self.dir / "first.wav", 1.0)
        second = make_wav(self.dir / "second.wav", 0.2)
        self.playback.play(first)
        GLib.timeout_add(100, lambda: self.playback.play(second) or False)
        self.run_loop()
        self.run_loop(1200)
        self.assertEqual([second], self.finished)
        self.assertEqual([], self.errors)

    def test_stop_suppresses_finished(self) -> None:
        wav = make_wav(self.dir / "a.wav", 0.3)
        self.playback.play(wav)
        self.playback.stop()
        self.run_loop(800)
        self.assertEqual([], self.finished)
        self.assertEqual([], self.errors)


if __name__ == "__main__":
    unittest.main()
