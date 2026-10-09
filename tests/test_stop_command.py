"""Команда `tts-player stop` (SIGUSR1, /tts-off): главный плеер стоп, звучащий свой текст — пауза;
порядок выхода в run() — свой текст раньше плеера, даже если его shutdown упал."""
import unittest

from tts_player.__main__ import run, stop_sound


class FakePlayer:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def stop_playback(self) -> None:
        self.calls.append("player.stop_playback")

    def start(self) -> None:
        self.calls.append("player.start")

    def shutdown(self) -> None:
        self.calls.append("player.shutdown")


class FakeTextSpeaker:
    def __init__(self, calls: list[str], shutdown_error: Exception | None = None) -> None:
        self.calls = calls
        self.shutdown_error = shutdown_error

    def pause(self) -> None:
        self.calls.append("text.pause")

    def cleanup_leftovers(self) -> None:
        self.calls.append("text.cleanup_leftovers")

    def shutdown(self) -> None:
        self.calls.append("text.shutdown")
        if self.shutdown_error is not None:
            raise self.shutdown_error


class FakeHookHome:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def stop(self) -> None:
        self.calls.append("hook_home.stop")


class StopCommandTest(unittest.TestCase):
    def test_stop_stops_player_and_pauses_text(self) -> None:
        calls: list[str] = []
        stop_sound(FakePlayer(calls), FakeTextSpeaker(calls))
        self.assertEqual(["player.stop_playback", "text.pause"], calls)

    def test_stop_without_text_speaker(self) -> None:
        calls: list[str] = []
        stop_sound(FakePlayer(calls), None)
        self.assertEqual(["player.stop_playback"], calls)

    def test_run_shuts_text_before_player(self) -> None:
        calls: list[str] = []
        run(FakePlayer(calls), lambda: None, lambda: None, FakeTextSpeaker(calls))
        self.assertEqual(["text.cleanup_leftovers", "player.start", "text.shutdown",
                          "player.shutdown"], calls)

    def test_player_shuts_down_when_text_shutdown_fails(self) -> None:
        calls: list[str] = []
        speaker = FakeTextSpeaker(calls, RuntimeError("сбой"))
        with self.assertRaises(RuntimeError):
            run(FakePlayer(calls), lambda: None, lambda: None, speaker)
        self.assertEqual(["text.shutdown", "player.shutdown"], calls[-2:])

    def test_hook_home_stops_last_even_if_player_shutdown_fails(self) -> None:
        calls: list[str] = []
        run(FakePlayer(calls), lambda: None, lambda: None, None, FakeHookHome(calls))
        self.assertEqual(["player.start", "player.shutdown", "hook_home.stop"], calls)
        calls.clear()
        player = FakePlayer(calls)
        player.shutdown = lambda: (_ for _ in ()).throw(RuntimeError("сбой"))
        with self.assertRaises(RuntimeError):
            run(player, lambda: None, lambda: None, FakeTextSpeaker(calls), FakeHookHome(calls))
        self.assertEqual("hook_home.stop", calls[-1])


if __name__ == "__main__":
    unittest.main()
