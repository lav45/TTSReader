"""Тесты TextSpeaker (контракт (10)): поддельный процесс синтеза — скрипт во временном каталоге,
звук — Playback с fakesink, главный плеер — запись hold/release или настоящий Player с
заглушкой Ducker (pactl не зовётся)."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from tts_player.player import Player  # noqa: E402
from tts_player.player_settings import Prefs  # noqa: E402
from tts_player.text_speaker import TextSpeaker, TextState  # noqa: E402

LOOP_LIMIT_S = 10.0

WORKER = '''
import json
import signal
import sys
import time
import wave
from pathlib import Path

PLAN = json.loads({plan!r})
LOG = {log!r}


def note(line):
    with open(LOG, "a", encoding="utf-8") as log:
        log.write(line + "\\n")


def on_term(signum, frame):
    note("term")
    sys.exit(143)


def emit(message):
    print(json.dumps(message, ensure_ascii=False), flush=True)


signal.signal(signal.SIGTERM, on_term)
out = Path(sys.argv[1])
note("start " + sys.stdin.buffer.read().decode("utf-8") + " " + out.name)
for step in PLAN:
    kind = step[0]
    if kind == "sleep":
        time.sleep(step[1])
    elif kind == "count":
        emit({{"count": step[1]}})
    elif kind == "wav":
        index, seconds = step[1], step[2]
        path = out / f"{{index}}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\\x00\\x00" * int(8000 * seconds))
        emit({{"index": index, "path": str(path), "duration": seconds}})
    elif kind == "done":
        emit({{"done": True}})
    elif kind == "error":
        emit({{"error": step[1]}})
    elif kind == "exit":
        sys.exit(step[1])
'''


class FakePlayer:
    """Подменяет Player: запоминает hold/release."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def hold(self) -> None:
        self.calls.append("hold")

    def release(self) -> None:
        self.calls.append("release")


class FakeDucker:
    """Подменяет Ducker: запоминает вызовы, pactl не зовёт никогда."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.has_orphans = False
        self.orphans_changed_at = None

    def duck(self, percent: int) -> None:
        self.calls.append(("duck", percent))

    def refresh(self) -> None:
        pass

    def watch(self) -> None:
        pass

    def restore(self) -> None:
        self.calls.append(("restore",))


class TextSpeakerCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.text_dir = self.runtime / "tts-player" / "text"
        self.log = self.root / "worker.log"
        self.worker = self.root / "fake_worker.py"
        self.player = FakePlayer()
        self.states: list[TextState] = []
        self.plays: list[tuple[str, float, float]] = []  # файл, start_s, длина в момент запуска

    def make(self, plan: list, player=None, python: Path | None = None) -> TextSpeaker:
        self.worker.write_text(WORKER.format(plan=json.dumps(plan), log=str(self.log)))
        speaker = TextSpeaker(self.runtime, python or Path(sys.executable), self.worker,
                              player or self.player, audio_sink="fakesink")
        self.addCleanup(speaker.shutdown)
        speaker.on_change = self.states.append
        playback = speaker._playback
        original = playback.play

        def play(path: Path, start_s: float = 0.0) -> None:
            self.plays.append((path.name, start_s, speaker.state().length_s))
            original(path, start_s)

        playback.play = play
        self.speaker = speaker
        return speaker

    def run_until(self, predicate, limit: float = LOOP_LIMIT_S) -> None:
        context = GLib.MainContext.default()
        deadline = time.monotonic() + limit
        while not predicate():
            if time.monotonic() > deadline:
                self.fail(f"не дождался; состояние {self.speaker.state()}")
            if not context.iteration(False):
                time.sleep(0.005)

    def status(self) -> str:
        return self.speaker.state().status

    def log_lines(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def starts(self) -> list[str]:
        return [line for line in self.log_lines() if line.startswith("start ")]


class TextSpeakerPlayTest(TextSpeakerCase):
    def test_initial_state_is_empty(self) -> None:
        speaker = self.make([])
        self.assertEqual(TextState("empty", 0.0, 0.0, False, ""), speaker.state())

    def test_blank_text_does_not_start(self) -> None:
        speaker = self.make([["count", 0], ["done"]])
        speaker.set_text("  \n")
        speaker.toggle_play_pause()
        self.assertEqual("empty", self.status())
        self.assertEqual([], self.player.calls)
        self.assertEqual([], self.starts())

    def test_play_holds_and_plays_first_before_last_arrives(self) -> None:
        speaker = self.make([["count", 3], ["wav", 0, 0.3], ["sleep", 0.6], ["wav", 1, 0.3],
                             ["sleep", 0.6], ["wav", 2, 0.3], ["done"]])
        speaker.set_text("Раз. Два. Три.")
        self.assertEqual("stopped", self.status())
        speaker.toggle_play_pause()
        self.assertEqual("synthesizing", self.status())
        self.assertEqual(["hold"], self.player.calls)
        self.run_until(lambda: self.status() == "stopped")
        self.assertEqual(["0.wav", "1.wav", "2.wav"], [name for name, _, _ in self.plays])
        self.assertAlmostEqual(0.3, self.plays[0][2])
        lengths = sorted({round(s.length_s, 3) for s in self.states})
        self.assertEqual([0.0, 0.3, 0.6, 0.9], lengths)
        self.assertIn("playing", [s.status for s in self.states if not s.complete])
        state = speaker.state()
        self.assertEqual(("stopped", 0.0, True, ""),
                         (state.status, state.position_s, state.complete, state.error))
        self.assertAlmostEqual(0.9, state.length_s)
        self.assertEqual(["hold", "release"], self.player.calls)
        self.assertEqual(["start Раз. Два. Три. 1"], self.starts())

    def test_replay_after_end_without_new_process(self) -> None:
        speaker = self.make([["count", 1], ["wav", 0, 0.2], ["done"]])
        speaker.set_text("Раз.")
        speaker.toggle_play_pause()
        self.run_until(lambda: self.status() == "stopped")
        speaker.set_text("Раз.")
        speaker.toggle_play_pause()
        self.assertEqual("playing", self.status())
        self.run_until(lambda: self.status() == "stopped")
        self.assertEqual(1, len(self.starts()))
        self.assertEqual(["0.wav", "0.wav"], [name for name, _, _ in self.plays])
        self.assertEqual(["hold", "release", "hold", "release"], self.player.calls)

    def test_pause_releases_and_resume_continues_from_position(self) -> None:
        speaker = self.make([["count", 1], ["wav", 0, 1.0], ["done"]])
        speaker.set_text("Раз.")
        speaker.toggle_play_pause()
        self.run_until(lambda: speaker.state().position_s > 0.2)
        speaker.toggle_play_pause()
        state = speaker.state()
        self.assertEqual("paused", state.status)
        self.assertEqual(["hold", "release"], self.player.calls)
        speaker.toggle_play_pause()
        self.assertEqual("playing", self.status())
        self.assertEqual(["hold", "release", "hold"], self.player.calls)
        self.assertAlmostEqual(state.position_s, self.plays[-1][1], places=2)
        self.run_until(lambda: self.status() == "stopped")
        self.assertEqual(["hold", "release", "hold", "release"], self.player.calls)

    def test_pause_during_synthesis_keeps_synthesizing(self) -> None:
        speaker = self.make([["count", 1], ["sleep", 0.3], ["wav", 0, 0.2], ["done"]])
        speaker.set_text("Раз.")
        speaker.toggle_play_pause()
        speaker.toggle_play_pause()
        self.assertEqual("paused", self.status())
        self.run_until(lambda: speaker.state().complete)
        self.assertEqual(TextState("paused", 0.0, 0.2, True, ""), speaker.state())
        self.assertEqual([], self.plays)
        speaker.toggle_play_pause()
        self.run_until(lambda: self.status() == "stopped")
        self.assertEqual(["0.wav"], [name for name, _, _ in self.plays])

    def test_seek_within_synthesized(self) -> None:
        speaker = self.make([["count", 2], ["wav", 0, 0.5], ["wav", 1, 0.5], ["done"]])
        speaker.set_text("Раз. Два.")
        speaker.toggle_play_pause()
        speaker.toggle_play_pause()
        self.run_until(lambda: speaker.state().complete)
        speaker.seek(5.0)
        self.assertAlmostEqual(1.0, speaker.state().position_s)
        speaker.seek(0.7)
        self.assertAlmostEqual(0.7, speaker.state().position_s)
        speaker.toggle_play_pause()
        name, start_s, _ = self.plays[-1]
        self.assertEqual("1.wav", name)
        self.assertAlmostEqual(0.2, start_s)
        self.run_until(lambda: self.status() == "stopped")

    def test_play_at_end_starts_from_beginning(self) -> None:
        speaker = self.make([["count", 1], ["wav", 0, 0.3], ["done"]])
        speaker.set_text("Раз.")
        speaker.toggle_play_pause()
        speaker.toggle_play_pause()
        self.run_until(lambda: speaker.state().complete)
        speaker.seek(0.3)
        speaker.toggle_play_pause()
        self.assertEqual(("0.wav", 0.0), self.plays[-1][:2])
        self.run_until(lambda: self.status() == "stopped")


class TextSpeakerCancelTest(TextSpeakerCase):
    def test_set_text_during_synthesis_terminates_and_removes_dir(self) -> None:
        speaker = self.make([["count", 2], ["sleep", 30]])
        speaker.set_text("Раз. Два.")
        speaker.toggle_play_pause()
        self.run_until(lambda: len(self.starts()) == 1)
        self.assertTrue((self.text_dir / "1").is_dir())
        speaker.set_text("Другой.")
        self.assertIn("term", self.log_lines())
        self.assertFalse((self.text_dir / "1").exists())
        self.assertEqual(TextState("stopped", 0.0, 0.0, False, ""), speaker.state())
        self.assertEqual(["hold", "release"], self.player.calls)
        speaker.toggle_play_pause()
        self.run_until(lambda: len(self.starts()) == 2)
        self.assertEqual("start Другой. 2", self.starts()[1])
        self.assertTrue((self.text_dir / "2").is_dir())

    def test_set_text_after_end_removes_dir(self) -> None:
        speaker = self.make([["count", 1], ["wav", 0, 0.2], ["done"]])
        speaker.set_text("Раз.")
        speaker.toggle_play_pause()
        self.run_until(lambda: self.status() == "stopped")
        speaker.set_text("Два.")
        self.assertFalse((self.text_dir / "1").exists())
        self.assertEqual(TextState("stopped", 0.0, 0.0, False, ""), speaker.state())
        speaker.set_text("")
        self.assertEqual("empty", self.status())

    def test_shutdown_kills_process_removes_dir_and_releases(self) -> None:
        speaker = self.make([["count", 2], ["sleep", 30]])
        speaker.set_text("Раз. Два.")
        speaker.toggle_play_pause()
        self.run_until(lambda: len(self.starts()) == 1)
        speaker.shutdown()
        self.assertIn("term", self.log_lines())
        self.assertFalse((self.text_dir / "1").exists())
        self.assertEqual(["hold", "release"], self.player.calls)

    def test_shutdown_while_stopped_releases(self) -> None:
        speaker = self.make([])
        speaker.shutdown()
        self.assertEqual(["release"], self.player.calls)

    def test_cleanup_leftovers_keeps_inbox(self) -> None:
        speaker = self.make([])
        (self.text_dir / "3").mkdir(parents=True)
        (self.text_dir / "3" / "0.wav").write_bytes(b"x")
        (self.text_dir / "4.part").write_bytes(b"x")
        inbox = self.runtime / "tts-player" / "inbox"
        inbox.mkdir()
        (inbox / "a.wav").write_bytes(b"x")
        speaker.cleanup_leftovers()
        self.assertEqual([], list(self.text_dir.iterdir()))
        self.assertTrue((inbox / "a.wav").exists())

    def test_cleanup_leftovers_without_text_dir(self) -> None:
        self.make([]).cleanup_leftovers()
        self.assertFalse(self.text_dir.exists())


class TextSpeakerErrorTest(TextSpeakerCase):
    def test_error_line_after_ready_sentence_plays_it(self) -> None:
        speaker = self.make([["count", 2], ["wav", 0, 0.3], ["error", "сбой синтеза"], ["exit", 1]])
        speaker.set_text("Раз. Два.")
        speaker.toggle_play_pause()
        self.run_until(lambda: self.status() == "error")
        state = speaker.state()
        self.assertEqual("сбой синтеза", state.error)
        self.assertFalse(state.complete)
        self.assertAlmostEqual(0.3, state.length_s)
        self.assertEqual(["0.wav"], [name for name, _, _ in self.plays])
        self.assertEqual(["hold", "release"], self.player.calls)

    def test_ready_sentence_replays_after_error_without_new_process(self) -> None:
        speaker = self.make([["count", 2], ["wav", 0, 0.2], ["error", "сбой синтеза"], ["exit", 1]])
        speaker.set_text("Раз. Два.")
        speaker.toggle_play_pause()
        self.run_until(lambda: self.status() == "error")
        speaker.toggle_play_pause()
        self.assertEqual("playing", self.status())
        self.run_until(lambda: self.status() == "error")
        self.assertEqual(["0.wav", "0.wav"], [name for name, _, _ in self.plays])
        self.assertEqual(1, len(self.starts()))

    def test_exit_2_with_error_line(self) -> None:
        speaker = self.make([["error", "import claude_code_tts: нет пакета"], ["exit", 2]])
        speaker.set_text("Раз.")
        speaker.toggle_play_pause()
        self.run_until(lambda: self.status() == "error")
        self.assertEqual("import claude_code_tts: нет пакета", speaker.state().error)
        self.assertEqual([], self.plays)
        self.assertEqual(["hold", "release"], self.player.calls)

    def test_play_after_error_without_sound_starts_new_process(self) -> None:
        speaker = self.make([["error", "нет голоса"], ["exit", 1]])
        speaker.set_text("Раз.")
        speaker.toggle_play_pause()
        self.run_until(lambda: self.status() == "error")
        self.assertTrue((self.text_dir / "1").exists())
        speaker.toggle_play_pause()
        self.assertEqual(TextState("synthesizing", 0.0, 0.0, False, ""), speaker.state())
        self.assertFalse((self.text_dir / "1").exists())
        self.run_until(lambda: self.status() == "error")
        self.assertEqual(2, len(self.starts()))

    def test_exit_code_without_error_line(self) -> None:
        speaker = self.make([["count", 1], ["exit", 3]])
        speaker.set_text("Раз.")
        speaker.toggle_play_pause()
        self.run_until(lambda: self.status() == "error")
        self.assertIn("3", speaker.state().error)
        self.assertEqual(["hold", "release"], self.player.calls)

    def test_exit_0_without_done(self) -> None:
        speaker = self.make([["count", 1]])
        speaker.set_text("Раз.")
        speaker.toggle_play_pause()
        self.run_until(lambda: self.status() == "error")
        self.assertNotEqual("", speaker.state().error)

    def test_process_not_started(self) -> None:
        speaker = self.make([], python=self.root / "нет-интерпретатора")
        speaker.set_text("Раз.")
        speaker.toggle_play_pause()
        state = speaker.state()
        self.assertEqual("error", state.status)
        self.assertNotEqual("", state.error)
        self.assertEqual([], self.player.calls)  # звучать нечему — главный плеер не тронут

    def test_garbage_line_is_error(self) -> None:
        speaker = self.make([["count", 1], ["sleep", 30]])
        self.worker.write_text('import sys, time\nsys.stdin.read()\nprint("шум", flush=True)\n'
                               'time.sleep(30)\n')
        speaker.set_text("Раз.")
        speaker.toggle_play_pause()
        self.run_until(lambda: self.status() == "error")
        self.assertIn("шум", speaker.state().error)


class TextSpeakerDuckingTest(TextSpeakerCase):
    """Приглушение своего текста — через главный Player в удержании и общий Ducker."""

    def setUp(self) -> None:
        super().setUp()
        self.ducker = FakeDucker()
        self.main = Player(self.runtime, "fakesink", None, None, ducker=self.ducker)
        self.addCleanup(self.main.shutdown)
        self.main.apply_prefs(Prefs(duck=45))

    def test_play_ducks_and_end_restores(self) -> None:
        speaker = self.make([["count", 1], ["wav", 0, 0.2], ["done"]], player=self.main)
        speaker.set_text("Раз.")
        speaker.toggle_play_pause()
        self.assertEqual([("duck", 45)], self.ducker.calls)
        self.run_until(lambda: self.status() == "stopped")
        self.assertEqual([("duck", 45), ("restore",)], self.ducker.calls)

    def test_pause_restores(self) -> None:
        speaker = self.make([["count", 1], ["wav", 0, 1.0], ["done"]], player=self.main)
        speaker.set_text("Раз.")
        speaker.toggle_play_pause()
        self.run_until(lambda: self.status() == "playing")
        speaker.toggle_play_pause()
        self.assertEqual([("duck", 45), ("restore",)], self.ducker.calls)

    def test_shutdown_restores(self) -> None:
        speaker = self.make([["count", 1], ["sleep", 30]], player=self.main)
        speaker.set_text("Раз.")
        speaker.toggle_play_pause()
        speaker.shutdown()
        self.assertEqual([("duck", 45), ("restore",)], self.ducker.calls)
        self.assertEqual("idle", self.main.state().status)


if __name__ == "__main__":
    unittest.main()
