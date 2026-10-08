"""Тесты Player: возврат незавершённого при штатном выходе, повтор при отказе вывода звука."""
import tempfile
import time
import unittest
from pathlib import Path
from typing import Callable
from unittest import mock

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from tts_player.__main__ import Player  # noqa: E402

A0 = "1000000000000000001_tts_queue_S_aa_s0.wav"
A1 = "1000000000000000002_tts_queue_S_aa_s1.wav"
B0 = "1000000000000000003_tts_queue_S_bb_s0.wav"


class FakePlayback:
    """Подменяет Playback: запоминает play(), колбэки зовёт тест."""

    def __init__(self, audio_sink: str | None = None) -> None:
        self.on_finished: Callable[[Path], None] = lambda path: None
        self.on_error: Callable[[Path, str, bool], None] = lambda path, text, output_failed: None
        self.played: list[str] = []
        self.stopped = False

    def play(self, path: Path) -> None:
        self.played.append(path.name)

    def stop(self) -> None:
        self.stopped = True


class PlayerTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runtime = Path(tmp.name)
        self.inbox_dir = self.runtime / "tts-player" / "inbox"
        self.store_dir = self.runtime / "tts-player" / "store"
        self.inbox_dir.mkdir(parents=True)
        patcher = mock.patch("tts_player.__main__.Playback", FakePlayback)
        patcher.start()
        self.addCleanup(patcher.stop)
        glib = mock.patch("tts_player.__main__.GLib")
        self.glib = glib.start()
        self.addCleanup(glib.stop)
        self.glib.timeout_add_seconds.return_value = 42
        self.player = Player(self.runtime, None)
        self.playback: FakePlayback = self.player._playback

    def start_with(self, *names: str) -> None:
        for name in names:
            (self.inbox_dir / name).write_bytes(b"wav")
        self.player.start()

    def path(self, name: str) -> Path:
        return self.store_dir / name

    def retry(self) -> None:
        """Срабатывание таймера повтора, поставленного последним."""
        seconds, callback, *data = self.glib.timeout_add_seconds.call_args.args
        self.assertEqual(2, seconds)
        callback(*data)

    def test_stop_returns_playing_and_unplayed_to_inbox(self) -> None:
        self.start_with(A0, A1, B0)
        self.assertEqual([A0], self.playback.played)
        self.player.stop()
        self.assertEqual(sorted([A0, A1, B0]), sorted(p.name for p in self.inbox_dir.iterdir()))
        self.assertEqual([], list(self.store_dir.iterdir()))

    def test_stop_deletes_finished(self) -> None:
        self.start_with(A0, A1, B0)
        self.playback.on_finished(self.path(A0))
        self.assertEqual([A0, A1], self.playback.played)
        self.player.stop()
        self.assertEqual(sorted([A1, B0]), sorted(p.name for p in self.inbox_dir.iterdir()))
        self.assertEqual([], list(self.store_dir.iterdir()))

    def test_file_error_skips_to_next(self) -> None:
        self.start_with(A0, B0)
        with self.assertLogs("tts_player", "INFO") as logs:
            self.playback.on_error(self.path(A0), "broken", False)
        self.assertEqual([A0, B0], self.playback.played)
        self.assertIn(f"error {A0}: broken", logs.output[0])
        self.glib.timeout_add_seconds.assert_not_called()

    def test_output_error_retries_same_sentence_and_reports_once(self) -> None:
        self.start_with(A0, B0)
        with self.assertLogs("tts_player", "INFO") as logs:
            self.playback.on_error(self.path(A0), "no output", True)
            self.assertEqual([A0], self.playback.played)
            self.retry()
            self.assertEqual([A0, A0], self.playback.played)
            self.playback.on_error(self.path(A0), "no output", True)
            self.retry()
            self.assertEqual([A0, A0, A0], self.playback.played)
            self.playback.on_finished(self.path(A0))
        self.assertEqual([A0, A0, A0, B0], self.playback.played)
        errors = [line for line in logs.output if "error " in line]
        self.assertEqual(1, len(errors), logs.output)
        self.assertIn(f"error {A0}: no output", errors[0])

    def test_new_outage_is_reported_again(self) -> None:
        self.start_with(A0, B0)
        with self.assertLogs("tts_player", "INFO") as logs:
            self.playback.on_error(self.path(A0), "no output", True)
            self.retry()
            self.playback.on_finished(self.path(A0))
            self.playback.on_error(self.path(B0), "no output", True)
        errors = [line for line in logs.output if "error " in line]
        self.assertEqual(2, len(errors), logs.output)

    def test_new_sentence_during_outage_does_not_jump_in(self) -> None:
        self.start_with(A0)
        self.playback.on_error(self.path(A0), "no output", True)
        (self.inbox_dir / B0).write_bytes(b"wav")
        deadline = time.monotonic() + 5
        while not self.path(B0).exists() and time.monotonic() < deadline:
            GLib.MainContext.default().iteration(False)
        self.assertTrue(self.path(B0).exists())
        self.assertEqual([A0], self.playback.played)

    def test_stop_cancels_pending_retry_and_returns_sentence(self) -> None:
        self.start_with(A0)
        self.playback.on_error(self.path(A0), "no output", True)
        self.player.stop()
        self.glib.source_remove.assert_called_once_with(42)
        self.assertEqual([A0], [p.name for p in self.inbox_dir.iterdir()])


if __name__ == "__main__":
    unittest.main()
