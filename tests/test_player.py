"""Тесты Player: воспроизведение по курсору, кнопки, снимок состояния, возврат
незавершённого при штатном выходе, повтор при отказе вывода звука, переход играющего
сообщения на склейку демона."""
import tempfile
import time
import unittest
import wave
from pathlib import Path
from typing import Callable
from unittest import mock

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from tts_player.history import HistoryEntry  # noqa: E402
from tts_player.inbox import NAME_PATTERN  # noqa: E402
from tts_player.model import Sentence  # noqa: E402
from tts_player.player import Player, PlayerState  # noqa: E402

A0 = "1000000000000000001_tts_queue_S_aa_s0.wav"
A1 = "1000000000000000002_tts_queue_S_aa_s1.wav"
B0 = "1000000000000000003_tts_queue_S_bb_s0.wav"
N0 = "1000000000000000004_tts_queue_N_cc_s0.wav"  # другая сессия: склейка S её не берёт
C0 = "1000000000000000005_tts_queue_S_cc_s0.wav"
JOINED = "tts-2000000000.000-S.wav"
RATE = 1000
SECONDS = 0.5


def write_wav(path: Path, seconds: float) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"\0\0" * int(seconds * RATE))


class FakePlayback:
    """Подменяет Playback: запоминает play(), колбэки зовёт тест."""

    def __init__(self, audio_sink: str | None = None) -> None:
        self.on_finished: Callable[[Path], None] = lambda path: None
        self.on_error: Callable[[Path, str, bool], None] = lambda path, text, output_failed: None
        self.played: list[str] = []
        self.starts: list[tuple[str, float]] = []
        self.at: float | None = None  # что вернёт position()
        self.playing: Path | None = None
        self.stops = 0

    def play(self, path: Path, start_s: float = 0.0) -> None:
        self.played.append(path.name)
        self.starts.append((path.name, start_s))
        self.playing = path

    def position(self) -> float | None:
        return self.at if self.playing is not None else None

    def stop(self) -> None:
        self.playing = None
        self.stops += 1

    def finish(self) -> None:
        """EOS играющего файла: как настоящий Playback — сначала stop, потом колбэк."""
        path, self.playing = self.playing, None
        self.on_finished(path)

    def fail(self, text: str, output_failed: bool) -> None:
        """Ошибка играющего файла: как настоящий Playback — сначала stop, потом колбэк."""
        path, self.playing = self.playing, None
        self.on_error(path, text, output_failed)


class PlayerCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runtime = Path(tmp.name)
        self.inbox_dir = self.runtime / "tts-player" / "inbox"
        self.store_dir = self.runtime / "tts-player" / "store"
        self.inbox_dir.mkdir(parents=True)
        history_dir = self.runtime / "speech_history"
        history_dir.mkdir()
        self.history_dir = history_dir
        patcher = mock.patch("tts_player.player.Playback", FakePlayback)
        patcher.start()
        self.addCleanup(patcher.stop)
        glib = mock.patch("tts_player.player.GLib")
        self.glib = glib.start()
        self.addCleanup(glib.stop)
        self.glib.timeout_add_seconds.return_value = 42
        self.player = Player(self.runtime, None, history_dir, self.runtime / "handy_analysis.db")
        self.playback: FakePlayback = self.player._playback
        self.states: list[PlayerState] = []
        self.player.on_change = self.states.append

    def start_with(self, *names: str) -> None:
        """Накопленное в inbox/ до старта: настоящие WAV по SECONDS."""
        for name in names:
            write_wav(self.inbox_dir / name, SECONDS)
        self.player.start()

    def arrive(self, name: str, seconds: float = SECONDS) -> None:
        """Предложение, забранное inbox/ после старта."""
        path = self.store_dir / name
        write_wav(path, seconds)
        match = NAME_PATTERN.match(name)
        self.player._on_sentence(Sentence(int(match["recv_ns"]), match["session_id"],
                                          match["token"], int(match["index"]), path))

    def path(self, name: str) -> Path:
        return self.store_dir / name

    def finish(self) -> None:
        """Играющий файл доиграл."""
        self.playback.finish()

    def retry(self) -> None:
        """Срабатывание таймера повтора, поставленного последним."""
        seconds, callback, *data = self.glib.timeout_add_seconds.call_args.args
        self.assertEqual(2, seconds)
        callback(*data)

    def joined(self, seconds: float = 1.0) -> HistoryEntry:
        """Склейка A0+A1 сессии S, как её пишет демон."""
        path = self.history_dir / JOINED
        write_wav(path, seconds)
        return HistoryEntry(path, 2_000_000_000.0, "S", "proj", seconds)

    def state(self) -> PlayerState:
        return self.player.state()

    def token(self) -> str | None:
        current = self.state().current
        return None if current is None else current.token


class PlayerHistoryTest(PlayerCase):
    """Склейка, отказ вывода, выход процесса — прежнее поведение."""

    def switch_on_a1(self) -> None:
        """Играет A1 (0,2 с от начала), приходит склейка A — переход на неё."""
        self.start_with(A0, A1, N0)
        self.finish()
        self.playback.at = 0.2
        self.player._on_entry(self.joined())

    def test_entry_switches_playing_message_at_its_position(self) -> None:
        with self.assertLogs("tts_player", "INFO") as logs:
            self.switch_on_a1()
        self.assertEqual([(A0, 0.0), (A1, 0.0), (JOINED, 0.7)], self.playback.starts)
        self.assertEqual(["complete S aa 1.00s", f"switched {JOINED} at 0.70"],
                         [r.getMessage() for r in logs.records
                          if r.getMessage().startswith(("complete", "switched"))])
        self.finish()
        self.assertEqual([A0, A1, JOINED, N0], self.playback.played)

    def test_entry_during_outage_waits_for_retry_of_joined_file(self) -> None:
        self.start_with(A0, A1, N0)
        self.finish()
        self.playback.fail("no output", True)
        self.player._on_entry(self.joined())
        self.assertEqual([A0, A1], self.playback.played)
        self.retry()
        self.assertEqual((JOINED, 0.5), self.playback.starts[-1])
        self.finish()
        self.assertEqual([A0, A1, JOINED, N0], self.playback.played)

    def test_entry_while_paused_is_remembered_and_resume_plays_joined(self) -> None:
        self.start_with(A0, A1, N0)
        self.finish()
        self.playback.at = 0.2
        self.player.toggle_pause()
        with self.assertLogs("tts_player", "INFO") as logs:
            self.player._on_entry(self.joined())
        self.assertEqual([], [r.getMessage() for r in logs.records
                              if r.getMessage().startswith("switched")])
        self.assertEqual("paused", self.state().status)
        self.player.toggle_pause()
        self.assertEqual(JOINED, self.playback.starts[-1][0])
        self.assertAlmostEqual(0.7, self.playback.starts[-1][1])

    def test_entry_of_message_waiting_at_its_end_moves_on(self) -> None:
        self.start_with(A0, A1)
        self.finish()
        self.finish()
        self.assertEqual("playing", self.state().status)
        self.player._on_entry(self.joined())
        self.assertEqual("idle", self.state().status)
        self.assertEqual("aa", self.token())
        self.assertTrue(self.state().current.heard)

    def test_stop_during_joined_file_returns_whole_message(self) -> None:
        self.switch_on_a1()
        self.player.stop()
        self.assertEqual(sorted([A0, A1, N0]), sorted(p.name for p in self.inbox_dir.iterdir()))
        self.assertEqual([], list(self.store_dir.iterdir()))

    def test_stop_after_joined_file_returns_only_next(self) -> None:
        self.switch_on_a1()
        self.finish()
        self.player.stop()
        self.assertEqual([N0], [p.name for p in self.inbox_dir.iterdir()])
        self.assertEqual([], list(self.store_dir.iterdir()))

    def test_store_is_taken_from_inbox(self) -> None:
        self.assertEqual(self.store_dir, self.player._inbox.store)

    def test_stop_returns_playing_and_unplayed_to_inbox(self) -> None:
        self.start_with(A0, A1, B0)
        self.assertEqual([A0], self.playback.played)
        self.player.stop()
        self.assertEqual(sorted([A0, A1, B0]), sorted(p.name for p in self.inbox_dir.iterdir()))
        self.assertEqual([], list(self.store_dir.iterdir()))

    def test_stop_deletes_finished(self) -> None:
        self.start_with(A0, A1, B0)
        self.finish()
        self.assertEqual([A0, A1], self.playback.played)
        self.player.stop()
        self.assertEqual(sorted([A1, B0]), sorted(p.name for p in self.inbox_dir.iterdir()))
        self.assertEqual([], list(self.store_dir.iterdir()))

    def test_stop_returns_rest_of_sentence_at_position(self) -> None:
        self.start_with(A0, A1, B0)
        self.playback.at = 0.3
        self.player.stop()
        self.assertEqual(sorted([A0, A1, B0]), sorted(p.name for p in self.inbox_dir.iterdir()))

    def test_file_error_skips_to_next(self) -> None:
        self.start_with(A0, B0)
        with self.assertLogs("tts_player", "INFO") as logs:
            self.playback.fail("broken", False)
        self.assertEqual([A0, B0], self.playback.played)
        self.assertIn(f"error {A0}: broken", logs.output[0])
        self.glib.timeout_add_seconds.assert_not_called()

    def test_broken_first_sentence_is_tried_not_skipped(self) -> None:
        (self.inbox_dir / A0).write_bytes(b"not a wav")
        write_wav(self.inbox_dir / A1, SECONDS)
        self.player.start()
        self.assertEqual([(A0, 0.0)], self.playback.starts)
        self.playback.fail("broken", False)
        self.assertEqual([(A0, 0.0), (A1, 0.0)], self.playback.starts)

    def test_output_error_retries_same_sentence_and_reports_once(self) -> None:
        self.start_with(A0, B0)
        with self.assertLogs("tts_player", "INFO") as logs:
            self.playback.fail("no output", True)
            self.assertTrue(self.state().output_down)
            self.assertEqual([A0], self.playback.played)
            self.retry()
            self.assertEqual([A0, A0], self.playback.played)
            self.playback.fail("no output", True)
            self.retry()
            self.assertEqual([A0, A0, A0], self.playback.played)
            self.finish()
        self.assertEqual([A0, A0, A0, B0], self.playback.played)
        self.assertFalse(self.state().output_down)
        errors = [line for line in logs.output if "error " in line]
        self.assertEqual(1, len(errors), logs.output)
        self.assertIn(f"error {A0}: no output", errors[0])
        playing = [line for line in logs.output if "playing " in line]
        self.assertEqual(1, len(playing), logs.output)  # повтор строки playing не пишет

    def test_new_outage_is_reported_again(self) -> None:
        self.start_with(A0, B0)
        with self.assertLogs("tts_player", "INFO") as logs:
            self.playback.fail("no output", True)
            self.retry()
            self.finish()
            self.playback.fail("no output", True)
        errors = [line for line in logs.output if "error " in line]
        self.assertEqual(2, len(errors), logs.output)

    def test_new_sentence_during_outage_does_not_jump_in(self) -> None:
        self.start_with(A0)
        self.playback.fail("no output", True)
        write_wav(self.inbox_dir / B0, SECONDS)
        deadline = time.monotonic() + 5
        while not self.path(B0).exists() and time.monotonic() < deadline:
            GLib.MainContext.default().iteration(False)
        self.assertTrue(self.path(B0).exists())
        self.assertEqual([A0], self.playback.played)

    def test_stop_cancels_pending_retry_and_returns_sentence(self) -> None:
        self.start_with(A0)
        self.playback.fail("no output", True)
        self.player.stop()
        self.glib.source_remove.assert_called_once_with(42)
        self.assertEqual([A0], [p.name for p in self.inbox_dir.iterdir()])

    def test_successful_retry_clears_outage_and_position_moves(self) -> None:
        self.start_with(A0, A1)
        self.playback.fail("no output", True)
        self.retry()
        self.assertTrue(self.state().output_down)  # повтор ещё не заиграл
        self.playback.at = 0.3
        state = self.state()
        self.assertFalse(state.output_down)
        self.assertAlmostEqual(0.3, state.position_s)
        self.player.toggle_pause()
        self.assertAlmostEqual(0.3, self.state().position_s)
        self.player.toggle_pause()
        self.assertEqual(A0, self.playback.starts[-1][0])
        self.assertAlmostEqual(0.3, self.playback.starts[-1][1])

    def test_retry_from_offset_is_not_progress(self) -> None:
        self.start_with(A0, A1)
        self.player.seek(0.2)
        self.playback.fail("no output", True)
        self.retry()
        self.playback.at = 0.2  # Playback до seek отдаёт позицию старта
        self.assertTrue(self.state().output_down)
        with self.assertNoLogs("tts_player", "INFO"):  # тот же отказ — без новой строки error
            self.playback.fail("no output", True)
        self.assertTrue(self.state().output_down)

    def test_next_during_outage_plays_and_recovers(self) -> None:
        self.start_with(A0, B0)
        self.playback.fail("no output", True)
        self.player.next()
        self.glib.source_remove.assert_called_once_with(42)
        self.assertEqual((B0, 0.0), self.playback.starts[-1])
        self.playback.at = 0.1
        state = self.state()
        self.assertEqual((False, "bb"), (state.output_down, state.current.token))
        self.assertAlmostEqual(0.1, state.position_s)
        self.player.stop()
        self.assertEqual([B0], [p.name for p in self.inbox_dir.iterdir()])

    def test_pause_during_outage_cancels_retry_and_resume_plays_same_place(self) -> None:
        self.start_with(A0, A1)
        self.finish()
        self.playback.fail("no output", True)
        self.player.toggle_pause()
        self.glib.source_remove.assert_called_once_with(42)
        state = self.state()
        self.assertEqual("paused", state.status)
        self.assertAlmostEqual(0.5, state.position_s)
        self.player.toggle_pause()
        self.assertEqual((A1, 0.0), self.playback.starts[-1])

    def test_seek_during_outage_plays_target(self) -> None:
        self.start_with(A0, A1)
        self.playback.fail("no output", True)
        self.player.seek(0.7)
        self.glib.source_remove.assert_called_once_with(42)
        name, start = self.playback.starts[-1]
        self.assertEqual(A1, name)
        self.assertAlmostEqual(0.2, start)
        self.playback.at = 0.4
        state = self.state()
        self.assertFalse(state.output_down)
        self.assertAlmostEqual(0.9, state.position_s)

    def test_heard_message_reached_by_prev_is_not_returned(self) -> None:
        self.start_with(A0, A1, B0)
        self.player.next()
        self.player.prev()
        self.player.toggle_play_stop()
        self.player.stop()
        self.assertEqual([], list(self.inbox_dir.iterdir()))
        self.assertEqual([], list(self.store_dir.iterdir()))

    def test_without_history_plays(self) -> None:
        player = Player(self.runtime, None, None, None)
        write_wav(self.inbox_dir / A0, SECONDS)
        player.start()
        self.assertEqual([A0], player._playback.played)
        player.stop()


class PlayerCursorTest(PlayerCase):
    """Автопереход и снимок состояния."""

    def test_idle_without_messages(self) -> None:
        self.player.start()
        state = self.state()
        self.assertEqual("idle", state.status)
        self.assertIsNone(state.current)
        self.assertEqual((0.0, 0.0, [], False),
                         (state.position_s, state.length_s, state.messages, state.output_down))

    def test_state_while_playing(self) -> None:
        self.start_with(A0, A1, B0)
        self.finish()
        self.playback.at = 0.2
        state = self.state()
        self.assertEqual(("playing", "aa"), (state.status, state.current.token))
        self.assertAlmostEqual(0.7, state.position_s)
        self.assertAlmostEqual(1.0, state.length_s)
        self.assertEqual(["aa", "bb"], [m.token for m in state.messages])

    def test_sentences_of_message_play_in_order_then_next_message(self) -> None:
        self.start_with(A0, A1, B0)
        self.finish()
        self.finish()
        self.assertEqual([A0, A1, B0], self.playback.played)
        self.assertTrue(self.state().messages[0].heard)

    def test_last_message_waits_for_more_sentences(self) -> None:
        self.start_with(A0)
        self.finish()
        state = self.state()
        self.assertEqual(("playing", "aa"), (state.status, state.current.token))
        self.assertAlmostEqual(0.5, state.position_s)
        self.arrive(A1)
        self.assertEqual([(A0, 0.0), (A1, 0.0)], self.playback.starts)
        self.assertAlmostEqual(0.5, self.state().position_s)

    def test_new_token_finishes_waiting_message(self) -> None:
        self.start_with(A0)
        self.finish()
        self.arrive(B0)
        self.assertEqual([A0, B0], self.playback.played)
        self.assertTrue(self.state().messages[0].heard)

    def test_idle_keeps_finished_current_and_new_message_plays(self) -> None:
        self.start_with(A0)
        self.finish()
        self.player._on_entry(self.joined(seconds=3.0))  # другая длина: конец без склейки
        state = self.state()
        self.assertEqual(("idle", "aa"), (state.status, state.current.token))
        self.arrive(B0)
        self.assertEqual(("playing", "bb"), (self.state().status, self.token()))
        self.assertEqual([A0, B0], self.playback.played)

    def test_tail_keeps_five_started_messages(self) -> None:
        names = [f"100000000000000000{i}_tts_queue_S_{t}_s0.wav"
                 for i, t in enumerate(["a1", "a2", "a3", "a4", "a5", "a6"], start=1)]
        self.start_with(*names)
        for _ in range(5):
            self.player.next()
        self.assertEqual(["a2", "a3", "a4", "a5", "a6"], [m.token for m in self.state().messages])
        self.assertFalse(self.path(names[0]).exists())

    def test_on_change_after_file_change_and_new_sentence(self) -> None:
        self.start_with(A0, A1)
        count = len(self.states)
        self.assertGreater(count, 0)
        self.finish()
        self.assertEqual(count + 1, len(self.states))
        self.arrive(B0)
        self.assertEqual(count + 2, len(self.states))
        self.assertEqual(["aa", "bb"], [m.token for m in self.states[-1].messages])


class PlayerButtonsTest(PlayerCase):
    def test_toggle_pause_keeps_position_and_resumes_from_it(self) -> None:
        self.start_with(A0, A1, B0)
        self.finish()
        self.playback.at = 0.2
        self.player.toggle_pause()
        self.assertIsNone(self.playback.playing)
        self.playback.at = 0.4
        state = self.state()
        self.assertEqual("paused", state.status)
        self.assertAlmostEqual(0.7, state.position_s)
        self.player.toggle_pause()
        self.assertEqual("playing", self.state().status)
        self.assertEqual(A1, self.playback.starts[-1][0])
        self.assertAlmostEqual(0.2, self.playback.starts[-1][1])

    def test_paused_player_does_not_move_on(self) -> None:
        self.start_with(A0)
        self.finish()
        self.player.toggle_pause()
        self.arrive(B0)
        self.assertEqual(("paused", "aa"), (self.state().status, self.token()))
        self.assertEqual([A0], self.playback.played)

    def test_play_stop_resets_position_and_new_message_does_not_start(self) -> None:
        self.start_with(A0, A1)
        self.finish()
        self.playback.at = 0.2
        self.player.toggle_play_stop()
        state = self.state()
        self.assertEqual(("stopped", 0.0), (state.status, state.position_s))
        self.assertIsNone(self.playback.playing)
        self.arrive(B0)
        self.assertEqual([A0, A1], self.playback.played)
        self.assertEqual("aa", self.token())
        self.player.toggle_play_stop()
        self.assertEqual("playing", self.state().status)
        self.assertEqual((A0, 0.0), self.playback.starts[-1])

    def test_play_stop_during_outage_cancels_retry(self) -> None:
        self.start_with(A0)
        self.playback.fail("no output", True)
        self.player.toggle_play_stop()
        self.glib.source_remove.assert_called_once_with(42)

    def test_next_plays_following_from_start_and_marks_left_heard(self) -> None:
        self.start_with(A0, A1, B0)
        self.playback.at = 0.3
        self.player.next()
        state = self.state()
        self.assertEqual(("playing", "bb", 0.0), (state.status, state.current.token,
                                                  self.player._position))
        self.assertEqual((B0, 0.0), self.playback.starts[-1])
        self.assertTrue(state.messages[0].heard)

    def test_next_on_last_does_nothing(self) -> None:
        self.start_with(A0, B0)
        self.player.next()
        played = list(self.playback.played)
        self.player.next()
        self.assertEqual(played, self.playback.played)
        self.assertEqual("bb", self.token())

    def test_next_after_stop_plays(self) -> None:
        self.start_with(A0, B0)
        self.player.toggle_play_stop()
        self.player.next()
        self.assertEqual(("playing", "bb"), (self.state().status, self.token()))
        self.assertEqual((B0, 0.0), self.playback.starts[-1])

    def test_prev_plays_preceding_from_start(self) -> None:
        self.start_with(A0, A1, B0)
        self.player.next()
        self.playback.at = 0.3
        self.player.prev()
        state = self.state()
        self.assertEqual(("playing", "aa"), (state.status, state.current.token))
        self.assertEqual((A0, 0.0), self.playback.starts[-1])
        self.assertTrue(state.messages[1].heard)

    def test_prev_on_first_does_nothing(self) -> None:
        self.start_with(A0, B0)
        self.player.prev()
        self.assertEqual([A0], self.playback.played)
        self.assertEqual("aa", self.token())

    def test_prev_while_paused_plays(self) -> None:
        self.start_with(A0, B0)
        self.player.next()
        self.player.toggle_pause()
        self.player.prev()
        self.assertEqual(("playing", "aa"), (self.state().status, self.token()))

    def test_seek_before_joined_plays_sentence_at_offset(self) -> None:
        self.start_with(A0, A1, B0)
        self.player.seek(0.7)
        name, start = self.playback.starts[-1]
        self.assertEqual(A1, name)
        self.assertAlmostEqual(0.2, start)
        self.playback.at = start
        self.assertAlmostEqual(0.7, self.state().position_s)
        self.finish()
        self.assertEqual(B0, self.playback.played[-1])

    def test_seek_on_pause_stays_paused_and_resume_starts_there(self) -> None:
        self.start_with(A0, A1, B0)
        self.player.toggle_pause()
        played = list(self.playback.played)
        self.player.seek(0.7)
        state = self.state()
        self.assertEqual("paused", state.status)
        self.assertAlmostEqual(0.7, state.position_s)
        self.assertEqual(played, self.playback.played)
        self.player.toggle_pause()
        name, start = self.playback.starts[-1]
        self.assertEqual(A1, name)
        self.assertAlmostEqual(0.2, start)

    def test_seek_clamps_to_message(self) -> None:
        self.start_with(A0, A1)
        self.player.toggle_pause()
        self.player.seek(99)
        self.assertAlmostEqual(1.0, self.state().position_s)
        self.player.seek(-1)
        self.assertEqual(0.0, self.state().position_s)

    def test_seek_after_joined_plays_joined_at_position(self) -> None:
        self.start_with(A0, A1, N0)
        self.player._on_entry(self.joined())
        self.player.seek(0.3)
        self.assertEqual(JOINED, self.playback.starts[-1][0])
        self.assertAlmostEqual(0.3, self.playback.starts[-1][1])

    def test_clear_empties_store_and_next_sentence_plays(self) -> None:
        self.start_with(A0, A1, B0)
        self.player.clear()
        state = self.state()
        self.assertEqual(("idle", None, []), (state.status, state.current, state.messages))
        self.assertIsNone(self.playback.playing)
        self.assertEqual([], list(self.store_dir.iterdir()))
        self.arrive(C0)
        self.assertEqual(("playing", "cc"), (self.state().status, self.token()))
        self.assertEqual(C0, self.playback.played[-1])

    def test_on_change_after_each_command(self) -> None:
        self.start_with(A0, A1, B0)
        commands = [self.player.toggle_pause, self.player.toggle_pause,
                    self.player.toggle_play_stop, self.player.toggle_play_stop,
                    self.player.next, self.player.prev,
                    lambda: self.player.seek(0.2), self.player.clear]
        for command in commands:
            count = len(self.states)
            command()
            self.assertEqual(count + 1, len(self.states), command)
            self.assertEqual(self.state(), self.states[-1])


if __name__ == "__main__":
    unittest.main()
