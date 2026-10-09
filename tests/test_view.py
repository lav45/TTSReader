import time
import unittest
from pathlib import Path

from tts_player.model import Message, Sentence
from tts_player.player import PlayerState
from tts_player.text_speaker import TextState
from tts_player.view import (next_enabled, play_pause_enabled, play_pause_label, position_label,
                             prev_enabled, rows, text_play_enabled, text_play_pause_label,
                             text_position_label)

CREATED_AT = 1_700_000_000.0


def hms(seconds: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(seconds))


def message(task="t", sentences=None, **kw) -> Message:
    return Message("s", task_ids={task}, sentences=sentences or [], **kw)


def state(status="idle", current=None, position_s=0.0, length_s=0.0, messages=(),
          lengths=None, completes=None) -> PlayerState:
    messages = list(messages)
    lengths = [0.0] * len(messages) if lengths is None else lengths
    completes = [True] * len(messages) if completes is None else completes
    return PlayerState(status, current, position_s, length_s, messages, False, lengths,
                       completes, True, [], None)


def sentence(recv_s: float) -> Sentence:
    return Sentence(int(recv_s * 1e9), "unknown", "s", "t", 0, Path("/nonexistent.wav"))


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

    def test_title_time_is_hook_task_arrival_before_and_after_sentences(self):
        """Сообщение есть в списке с прихода задачи хука; время не прыгает ни с первым
        предложением, ни с переозвучкой."""
        m = message(received_at=CREATED_AT, project="p")
        self.assertEqual(f"{hms(CREATED_AT)}  p", rows(state(messages=[m]))[0].title)
        m.sentences.append(sentence(CREATED_AT + 700))
        self.assertEqual(f"{hms(CREATED_AT)}  p", rows(state(messages=[m]))[0].title)

    def test_message_without_sentences_is_pending_with_zero_length(self):
        m = message(received_at=CREATED_AT)
        row = rows(state(messages=[m], lengths=[0.0], completes=[False]))[0]
        self.assertEqual("0:00…", row.duration)

    def test_ellipsis_by_completeness_not_duration(self):
        done = message("a", duration_s=None)
        growing = message("b", duration_s=7)
        result = rows(state(messages=[done, growing], lengths=[4.0, 7.0],
                            completes=[True, False]))
        self.assertEqual([r.duration for r in result], ["0:04", "0:07…"])

    def test_title_time_from_first_sentence(self):
        s = Sentence(int(CREATED_AT * 1e9), "unknown", "s", "t", 0, Path("/nonexistent.wav"))
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

    def test_play_pause_per_status(self):
        m = message()
        expected = {"playing": "⏸", "paused": "⏵", "stopped": "⏵", "idle": "⏵"}
        for status, label in expected.items():
            self.assertEqual(play_pause_label(state(status, m)), label, status)


class ControlsTest(unittest.TestCase):
    def test_play_pause_enabled_with_current(self):
        m = message(heard=True)
        for status in ("playing", "paused", "stopped", "idle"):
            self.assertTrue(play_pause_enabled(state(status, m, messages=[m])), status)

    def test_play_pause_enabled_idle_without_messages(self):
        self.assertFalse(play_pause_enabled(state("idle")))

    def test_play_pause_enabled_idle_with_unheard_only(self):
        heard, unheard = message("a", heard=True), message("b")
        self.assertTrue(play_pause_enabled(state("idle", messages=[heard, unheard])))
        self.assertFalse(play_pause_enabled(state("idle", messages=[heard])))

    def test_prev_next_at_edges(self):
        a, b, c = message("a"), message("b"), message("c")
        cases = {a: (False, True), b: (True, True), c: (True, False)}
        for current, (prev, following) in cases.items():
            st = state("playing", current, messages=[a, b, c])
            self.assertEqual((prev_enabled(st), next_enabled(st)), (prev, following),
                             current.task_ids)

    def test_prev_next_without_current(self):
        st = state(messages=[message("a"), message("b")])
        self.assertEqual((prev_enabled(st), next_enabled(st)), (False, False))

if __name__ == "__main__":
    unittest.main()


class TextViewTest(unittest.TestCase):
    """Экран «Свой текст»: подписи и доступность по TextState."""

    @staticmethod
    def text(status: str, position_s: float = 0.0, length_s: float = 0.0) -> TextState:
        return TextState(status, position_s, length_s, "")

    def test_play_pause_label_per_status(self):
        for status, label in (("synthesizing", "⏸"), ("playing", "⏸"), ("paused", "⏵"),
                              ("stopped", "⏵"), ("error", "⏵"), ("empty", "⏵")):
            self.assertEqual(label, text_play_pause_label(self.text(status)), status)

    def test_play_enabled_unless_empty(self):
        for status in ("synthesizing", "playing", "paused", "stopped", "error"):
            self.assertTrue(text_play_enabled(self.text(status)), status)
        self.assertFalse(text_play_enabled(self.text("empty")))

    def test_position(self):
        self.assertEqual("1:05 / 1:02:05", text_position_label(self.text("playing", 65.0, 3725.0)))
