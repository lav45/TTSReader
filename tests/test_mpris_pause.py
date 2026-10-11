"""Тесты MprisPause: поддельные плееры на временной шине Gio.TestDBus (tests/mpris_fake.py)."""
import tests  # noqa: F401 — окружение тестов (tests/__init__.py)
import time
import unittest
from unittest import mock

from gi.repository import GLib

from tests.mpris_fake import ANSWER, ERROR, SILENT, FakeBus, FakePlayer
from tts_player.mpris_pause import CALL_TIMEOUT_MS, MprisPause


def spin(until=None, limit_ms=300) -> bool:
    """Настоящий цикл GLib: до until (не дольше 5 с) или limit_ms, если until нет. Выполнено ли until."""
    loop = GLib.MainLoop()
    GLib.timeout_add(5000 if until else limit_ms, loop.quit)
    if until:
        def check():
            if until():
                loop.quit()
                return False
            return True
        GLib.timeout_add(10, check)
    loop.run()
    return bool(until and until())


class MprisPauseTest(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()
        self.addCleanup(self.bus.down)
        self.pause = MprisPause(self.bus.connect())
        self.addCleanup(self.pause.stop)
        self.results = []

    def player(self, name, status, mode=ANSWER):
        return FakePlayer(self.bus, name, status, mode)

    def pause_playing(self):
        self.pause.pause_playing(self.results.append)
        spin(lambda: self.results)

    def test_pauses_only_playing_and_resumes_only_it(self):
        playing = self.player("a", "Playing")
        paused = self.player("b", "Paused")
        self.pause_playing()
        self.assertEqual([True], self.results)
        self.assertEqual(["Pause"], playing.calls)
        self.assertEqual([], paused.calls)
        self.assertTrue(self.pause.holding)
        self.pause.resume()
        spin(lambda: playing.status == "Playing")
        self.assertEqual(["Pause", "Play"], playing.calls)
        self.assertEqual([], paused.calls)
        self.assertFalse(self.pause.holding)

    def test_nobody_playing(self):
        stopped = self.player("a", "Stopped")
        self.pause_playing()
        self.assertEqual([False], self.results)
        self.assertEqual([], stopped.calls)
        self.assertFalse(self.pause.holding)

    def test_no_mpris_names(self):
        self.pause_playing()
        self.assertEqual([False], self.results)

    def test_dbus_error_not_remembered_and_logged(self):
        broken = self.player("a", "Playing", ERROR)
        with self.assertLogs("tts_player.mpris_pause", "WARNING") as logs:
            self.pause_playing()
        self.assertEqual([False], self.results)
        self.assertEqual(["Pause"], broken.calls)
        self.assertFalse(self.pause.holding)
        self.assertEqual(1, len(logs.records))

    def test_silent_player_times_out_without_delaying_done_past_timeout(self):
        silent = self.player("a", "Playing", SILENT)
        good = self.player("b", "Playing")
        started = time.monotonic()
        with self.assertLogs("tts_player.mpris_pause", "WARNING"):
            self.pause_playing()
        elapsed = time.monotonic() - started
        self.assertEqual([True], self.results)
        self.assertLess(elapsed, CALL_TIMEOUT_MS / 1000 + 0.5)
        self.assertEqual(["Pause"], silent.calls)
        self.assertEqual(["Pause"], good.calls)
        self.pause.resume()
        spin(lambda: good.status == "Playing")
        self.assertEqual(["Pause"], silent.calls)

    def test_only_silent_player_gives_false(self):
        self.player("a", "Playing", SILENT)
        with self.assertLogs("tts_player.mpris_pause", "WARNING"):
            self.pause_playing()
        self.assertEqual([False], self.results)
        self.assertFalse(self.pause.holding)

    def test_user_resumed_player_is_not_played_again(self):
        player = self.player("a", "Playing")
        self.pause_playing()
        player.set_status("Playing")
        self.assertTrue(spin(lambda: not self.pause.holding))
        player.set_status("Paused")
        spin()
        self.pause.resume()
        spin()
        self.assertEqual(["Pause"], player.calls)

    def test_stopped_player_is_not_touched(self):
        player = self.player("a", "Playing")
        self.pause_playing()
        player.set_status("Stopped")
        self.assertTrue(spin(lambda: not self.pause.holding))
        player.set_status("Paused")
        spin()
        self.pause.resume()
        spin()
        self.assertEqual(["Pause"], player.calls)

    def test_vanished_player_is_forgotten(self):
        player = self.player("a", "Playing")
        self.pause_playing()
        player.vanish()
        self.assertTrue(spin(lambda: not self.pause.holding))

    def test_resume_twice_and_without_held_makes_no_calls(self):
        player = self.player("a", "Playing")
        self.pause_playing()
        self.pause.resume()
        spin(lambda: player.status == "Playing")
        self.pause.resume()
        spin()
        self.assertEqual(["Pause", "Play"], player.calls)
        fresh = MprisPause(self.bus.connect())
        self.addCleanup(fresh.stop)
        fresh.resume()
        spin()
        self.assertEqual(["Pause", "Play"], player.calls)

    def test_stop_unsubscribes_everything_even_after_overlapping_calls(self):
        player = self.player("a", "Playing")
        self.pause.pause_playing(self.results.append)
        self.pause.pause_playing(self.results.append)
        self.assertTrue(spin(lambda: len(self.results) == 2))
        self.assertEqual(["Pause"], player.calls)
        self.assertEqual([True, False], sorted(self.results, reverse=True))
        self.pause.stop()
        self.assertFalse(self.pause.holding)
        with mock.patch.object(self.pause, "_forget") as forget:
            player.set_status("Playing")
            player.vanish()
            spin()
        forget.assert_not_called()

    def test_resume_then_stop_still_sends_play(self):
        player = self.player("a", "Playing")
        self.pause_playing()
        self.pause.resume()
        self.pause.stop()
        self.assertTrue(spin(lambda: player.status == "Playing"))
        self.assertEqual(["Pause", "Play"], player.calls)

    def test_resume_while_pause_pending_plays_when_pause_lands(self):
        player = self.player("a", "Playing", SILENT)
        self.pause.pause_playing(self.results.append)
        self.assertTrue(spin(lambda: player.calls))
        self.pause.resume()
        self.assertFalse(self.pause.holding)
        player.answer_pending()
        self.assertTrue(spin(lambda: player.status == "Playing"))
        self.assertEqual(["Pause", "Play"], player.calls)

    def test_resume_then_stop_while_pause_pending_plays_when_pause_lands(self):
        player = self.player("a", "Playing", SILENT)
        self.pause.pause_playing(self.results.append)
        self.assertTrue(spin(lambda: player.calls))
        self.pause.resume()
        self.pause.stop()
        player.answer_pending()
        self.assertTrue(spin(lambda: player.status == "Playing"))
        self.assertEqual([], self.results)

    def test_stop_during_pending_call_means_no_done(self):
        self.player("a", "Playing", SILENT)
        self.pause.pause_playing(self.results.append)
        spin()
        self.pause.stop()
        with self.assertLogs("tts_player.mpris_pause", "WARNING") as logs:
            spin(limit_ms=CALL_TIMEOUT_MS + 500)
        self.assertIn("Pause", logs.output[0])
        self.assertEqual([], self.results)


if __name__ == "__main__":
    unittest.main()
