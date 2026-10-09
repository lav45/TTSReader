import tempfile
import time
import unittest
import wave
from pathlib import Path

from tts_player.model import Message, Sentence
from tts_player.player import PlayerState
from tts_player.view import play_stop_label, pause_label, position_label, rows

CREATED_AT = 1_700_000_000.0


def hms(seconds: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(seconds))


def write_wav(path: Path, seconds: int) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(1000)
        w.writeframes(b"\0\0" * 1000 * seconds)
    return path


def message(token="t", sentences=None, **kw) -> Message:
    return Message("s", token, sentences or [], **kw)


def state(status="idle", current=None, position_s=0.0, length_s=0.0, messages=()) -> PlayerState:
    return PlayerState(status, current, position_s, length_s, list(messages), False)


class RowsTest(unittest.TestCase):
    def test_order_current_and_heard(self):
        a = message("a", created_at=CREATED_AT, duration_s=5, heard=True)
        b = message("b", created_at=CREATED_AT, duration_s=5)
        result = rows(state("playing", b, messages=[a, b]))
        self.assertEqual([r.current for r in result], [False, True])
        self.assertEqual([r.heard for r in result], [True, False])

    def test_title_with_created_at_and_project(self):
        m = message(created_at=CREATED_AT, project="/home/u/proj/", duration_s=5)
        self.assertEqual(rows(state(messages=[m]))[0].title, f"{hms(CREATED_AT)}  proj")

    def test_title_without_project(self):
        for project in (None, "", "/"):
            m = message(created_at=CREATED_AT, project=project, duration_s=5)
            self.assertEqual(rows(state(messages=[m]))[0].title, f"{hms(CREATED_AT)}  —")

    def test_title_time_from_first_sentence(self):
        s = Sentence(int(CREATED_AT * 1e9), "s", "t", 0, Path("/nonexistent.wav"))
        m = message(sentences=[s], project="p")
        self.assertEqual(rows(state(messages=[m]))[0].title, f"{hms(CREATED_AT)}  p")

    def test_duration_known_has_no_ellipsis(self):
        m = message(created_at=CREATED_AT, duration_s=65)
        self.assertEqual(rows(state(messages=[m]))[0].duration, "1:05")

    def test_duration_hours(self):
        m = message(created_at=CREATED_AT, duration_s=3725)
        self.assertEqual(rows(state(messages=[m]))[0].duration, "1:02:05")

    def test_duration_before_gluing_sums_sentences(self):
        with tempfile.TemporaryDirectory() as d:
            sentences = [
                Sentence(1, "s", "t", i, write_wav(Path(d) / f"{i}.wav", 2)) for i in range(2)
            ]
            m = message(sentences=sentences)
            self.assertEqual(rows(state(messages=[m]))[0].duration, "0:04…")

    def test_unreadable_sentence_counts_zero(self):
        s = Sentence(1, "s", "t", 0, Path("/nonexistent/x.wav"))
        m = message(sentences=[s])
        self.assertEqual(rows(state(messages=[m]))[0].duration, "0:00…")


class LabelsTest(unittest.TestCase):
    def test_position(self):
        m = message()
        self.assertEqual(position_label(state("playing", m, 5.4, 125.0)), "0:05 / 2:05")

    def test_position_without_current(self):
        self.assertEqual(position_label(state()), "0:00 / 0:00")

    def test_buttons_per_status(self):
        m = message()
        expected = {
            "playing": ("■", "⏸"),
            "paused": ("■", "⏵"),
            "stopped": ("▶", "⏸"),
            "idle": ("▶", "⏸"),
        }
        for status, (play, pause) in expected.items():
            st = state(status, m)
            self.assertEqual((play_stop_label(st), pause_label(st)), (play, pause), status)


if __name__ == "__main__":
    unittest.main()
