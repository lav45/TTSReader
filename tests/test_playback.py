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
        self.errors: list[tuple[Path, str, bool]] = []
        self.playback = Playback("fakesink")
        self.addCleanup(self.playback.stop)
        self.playback.on_finished = self._finished
        self.playback.on_error = self._errored

    def _finished(self, path: Path) -> None:
        self.finished.append(path)
        self.loop.quit()

    def _errored(self, path: Path, text: str, output_failed: bool) -> None:
        self.errors.append((path, text, output_failed))
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
        with self.assertNoLogs("tts_player.playback", level="INFO"):
            self.playback.play(bad)
            self.run_loop()
        self.assertEqual([], self.finished)
        self.assertEqual(1, len(self.errors))
        self.assertEqual(bad, self.errors[0][0])
        self.assertTrue(self.errors[0][1])
        self.assertFalse(self.errors[0][2])

    def test_missing_file_is_file_error(self) -> None:
        missing = self.dir / "missing.wav"
        self.playback.play(missing)
        self.run_loop()
        self.assertEqual([(missing, False)], [(p, out) for p, _, out in self.errors])

    def test_sink_that_fails_to_open_is_output_error(self) -> None:
        # filesink без location не открывается: ResourceError от sink, звука нет
        playback = Playback("filesink")
        self.addCleanup(playback.stop)
        playback.on_finished = self._finished
        playback.on_error = self._errored
        wav = make_wav(self.dir / "a.wav", 0.2)
        playback.play(wav)
        self.run_loop()
        self.assertEqual([], self.finished)
        self.assertEqual(1, len(self.errors))
        self.assertEqual(wav, self.errors[0][0])
        self.assertTrue(self.errors[0][1])
        self.assertTrue(self.errors[0][2])

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
    def test_start_offset_shortens_playback(self) -> None:
        wav = make_wav(self.dir / "a.wav", 1.0)
        started = time.monotonic()
        self.playback.play(wav, start_s=0.6)
        self.run_loop()
        elapsed = time.monotonic() - started
        self.assertEqual([wav], self.finished)
        self.assertEqual([], self.errors)
        self.assertAlmostEqual(0.4, elapsed, delta=0.15)

    def test_start_beyond_end_finishes_without_error(self) -> None:
        wav = make_wav(self.dir / "a.wav", 0.3)
        self.playback.play(wav, start_s=5.0)
        self.run_loop()
        self.assertEqual([wav], self.finished)
        self.assertEqual([], self.errors)

    def test_position_grows_within_file_and_none_after_stop(self) -> None:
        duration = 1.5
        wav = make_wav(self.dir / "a.wav", duration)
        samples: list[float | None] = []
        self.playback.play(wav)
        for delay in (300, 700):
            GLib.timeout_add(delay, lambda: samples.append(self.playback.position()) or False)
        self.run_loop(1000)
        first, second = samples
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertLess(first, second)
        self.assertGreaterEqual(first, 0.0)
        self.assertLessEqual(second, duration)
        self.playback.stop()
        self.assertIsNone(self.playback.position())

    def test_position_none_when_idle(self) -> None:
        self.assertIsNone(self.playback.position())

    def test_position_after_start_offset_begins_at_offset(self) -> None:
        wav = make_wav(self.dir / "a.wav", 2.0)
        samples: list[float | None] = []
        self.playback.play(wav, start_s=1.0)
        GLib.timeout_add(300, lambda: samples.append(self.playback.position()) or False)
        self.run_loop(600)
        self.assertGreaterEqual(samples[0], 1.0)


if __name__ == "__main__":
    unittest.main()
