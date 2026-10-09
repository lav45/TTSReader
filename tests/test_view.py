import time
import unittest
from pathlib import Path

from tts_player.model import Message, Sentence
from tts_player.player import PlayerState
from tts_player.view import (current_title, next_enabled, pause_enabled, play_stop_label,
                             pause_label, position_label, prev_enabled, rows)

CREATED_AT = 1_700_000_000.0


def hms(seconds: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(seconds))


def message(token="t", sentences=None, **kw) -> Message:
    return Message("s", token, sentences or [], **kw)


def state(status="idle", current=None, position_s=0.0, length_s=0.0, messages=(),
          lengths=None, completes=None) -> PlayerState:
    messages = list(messages)
    lengths = [0.0] * len(messages) if lengths is None else lengths
    completes = [True] * len(messages) if completes is None else completes
    return PlayerState(status, current, position_s, length_s, messages, False, lengths,
                       completes)


def sentence(recv_s: float) -> Sentence:
    return Sentence(int(recv_s * 1e9), "s", "t", 0, Path("/nonexistent.wav"))


class RowsTest(unittest.TestCase):
    def test_order_current_and_heard(self):
        a = message("a", created_at=CREATED_AT, duration_s=5, heard=True)
        b = message("b", created_at=CREATED_AT, duration_s=5)
        result = rows(state("playing", b, messages=[a, b]))
        self.assertEqual([r.current for r in result], [False, True])
        self.assertEqual([r.heard for r in result], [True, False])

    def test_title_with_created_at_and_project(self):
        m = message(sentences=[sentence(CREATED_AT)], project="/home/u/proj/", duration_s=5)
        self.assertEqual(rows(state(messages=[m]))[0].title, f"{hms(CREATED_AT)}  proj")

    def test_title_without_project(self):
        for project in (None, "", "/"):
            m = message(sentences=[sentence(CREATED_AT)], project=project, duration_s=5)
            self.assertEqual(rows(state(messages=[m]))[0].title, f"{hms(CREATED_AT)}  —")

    def test_title_time_is_first_sentence_even_with_created_at(self):
        m = message(sentences=[sentence(CREATED_AT)], created_at=CREATED_AT + 500, project="p")
        before = rows(state(messages=[m]))[0].title
        self.assertEqual(before, f"{hms(CREATED_AT)}  p")
        m.created_at = CREATED_AT + 900
        self.assertEqual(rows(state(messages=[m]))[0].title, before)

    def test_ellipsis_by_completeness_not_duration(self):
        done = message("a", duration_s=None)
        growing = message("b", duration_s=7)
        result = rows(state(messages=[done, growing], lengths=[4.0, 7.0],
                            completes=[True, False]))
        self.assertEqual([r.duration for r in result], ["0:04", "0:07…"])

    def test_title_time_from_first_sentence(self):
        s = Sentence(int(CREATED_AT * 1e9), "s", "t", 0, Path("/nonexistent.wav"))
        m = message(sentences=[s], project="p")
        self.assertEqual(rows(state(messages=[m]))[0].title, f"{hms(CREATED_AT)}  p")

    def test_duration_known_has_no_ellipsis(self):
        m = message(created_at=CREATED_AT, duration_s=65)
        self.assertEqual(rows(state(messages=[m], lengths=[65.0]))[0].duration, "1:05")

    def test_duration_hours(self):
        m = message(created_at=CREATED_AT, duration_s=3725)
        self.assertEqual(rows(state(messages=[m], lengths=[3725.0]))[0].duration, "1:02:05")

    def test_duration_before_gluing_has_ellipsis(self):
        m = message()
        result = rows(state(messages=[m], lengths=[4.0], completes=[False]))
        self.assertEqual(result[0].duration, "0:04…")

    def test_duration_uses_length_of_own_message(self):
        a, b = message("a"), message("b", duration_s=9)
        result = rows(state(messages=[a, b], lengths=[4.0, 9.0], completes=[False, True]))
        self.assertEqual([r.duration for r in result], ["0:04…", "0:09"])


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


class ControlsTest(unittest.TestCase):
    def test_pause_only_while_playing_or_paused(self):
        m = message()
        expected = {"playing": True, "paused": True, "stopped": False, "idle": False}
        for status, enabled in expected.items():
            self.assertEqual(pause_enabled(state(status, m, messages=[m])), enabled, status)

    def test_prev_next_at_edges(self):
        a, b, c = message("a"), message("b"), message("c")
        cases = {a: (False, True), b: (True, True), c: (True, False)}
        for current, (prev, following) in cases.items():
            st = state("playing", current, messages=[a, b, c])
            self.assertEqual((prev_enabled(st), next_enabled(st)), (prev, following),
                             current.token)

    def test_prev_next_without_current(self):
        st = state(messages=[message("a"), message("b")])
        self.assertEqual((prev_enabled(st), next_enabled(st)), (False, False))

    def test_current_title(self):
        a = message("a", [sentence(CREATED_AT)], project="p")
        b = message("b", [sentence(CREATED_AT + 1)], project="q")
        self.assertEqual(current_title(state("playing", b, messages=[a, b])),
                         f"{hms(CREATED_AT + 1)}  q")

    def test_current_title_without_current(self):
        self.assertEqual(current_title(state(messages=[message("a")])), "")


if __name__ == "__main__":
    unittest.main()
