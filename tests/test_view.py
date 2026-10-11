import tests  # noqa: F401 — окружение тестов (tests/__init__.py)
import time
import unittest
from pathlib import Path

from tts_player.model import Message, Sentence
from tts_player.player import PlayerState
from tts_player.sessions import Session
from tts_player.text_speaker import TextState
from tts_player.view import (MessageRow, SessionRow, clear_enabled, play_pause_enabled,
                             play_pause_label, position_label, rows, session_rows,
                             stop_enabled, text_play_enabled,
                             text_play_pause_label, text_position_label, tree_rows)

CREATED_AT = 1_700_000_000.0


def hms(seconds: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(seconds))


def message(task="t", sentences=None, **kw) -> Message:
    return Message("s", task_ids={task}, sentences=sentences or [], **kw)


def state(status="idle", current=None, position_s=0.0, length_s=0.0, messages=(),
          lengths=None, sessions=(), active=None) -> PlayerState:
    messages = list(messages)
    lengths = [0.0] * len(messages) if lengths is None else lengths
    return PlayerState(status, current, position_s, length_s, messages, False, lengths,
                       list(sessions), active)


def sentence(recv_s: float) -> Sentence:
    return Sentence(int(recv_s * 1e9), "unknown", "s", "t", 0, Path("/nonexistent.wav"))


class RowsTest(unittest.TestCase):
    def test_order_index_current_and_heard(self):
        a = message("a", heard=True)
        b = message("b")
        result = rows(state("playing", b, messages=[a, b]))
        self.assertEqual([r.index for r in result], [0, 1])
        self.assertEqual([r.current for r in result], [False, True])
        self.assertEqual([r.heard for r in result], [True, False])

    def test_time_is_own_column(self):
        m = message(received_at=CREATED_AT, project="p", text="Готово")
        row = rows(state(messages=[m]))[0]
        self.assertEqual(("Готово", hms(CREATED_AT)), (row.title, row.time))

    def test_title_without_text_is_project(self):
        for text in (None, "", "  \n"):
            m = message(received_at=CREATED_AT, project="/home/u/proj/", text=text)
            self.assertEqual("proj", rows(state(messages=[m]))[0].title)

    def test_title_without_text_and_project(self):
        for project in (None, "", "/"):
            m = message(sentences=[sentence(CREATED_AT)], project=project)
            self.assertEqual("—", rows(state(messages=[m]))[0].title)

    def test_title_is_first_words_of_text(self):
        m = message(received_at=CREATED_AT, project="p", text="Готово:  тесты\nпрошли.")
        self.assertEqual("Готово: тесты прошли.", rows(state(messages=[m]))[0].title)

    def test_title_text_cut_to_first_words(self):
        text = "один два три четыре пять шесть семь восемь девять десять"
        m = message(received_at=CREATED_AT, project="p", text=text)
        self.assertEqual("один два три четыре пять шесть семь восемь…",
                         rows(state(messages=[m]))[0].title)

    def test_time_without_hook_task_arrival_is_first_sentence(self):
        m = message(sentences=[sentence(CREATED_AT), sentence(CREATED_AT + 500)], project="p")
        self.assertEqual(hms(CREATED_AT), rows(state(messages=[m]))[0].time)

    def test_time_is_hook_task_arrival_before_and_after_sentences(self):
        """Сообщение есть в списке с прихода задачи хука; время не прыгает ни с первым
        предложением, ни с переозвучкой."""
        m = message(received_at=CREATED_AT, project="p")
        self.assertEqual(hms(CREATED_AT), rows(state(messages=[m]))[0].time)
        m.sentences.append(sentence(CREATED_AT + 700))
        self.assertEqual(hms(CREATED_AT), rows(state(messages=[m]))[0].time)

    def test_time_unknown(self):
        self.assertEqual("--:--:--", rows(state(messages=[message()]))[0].time)

    def test_duration_has_no_pending_mark(self):
        growing, deferred = message("a"), message("b", deferred=True)
        result = rows(state(messages=[growing, deferred], lengths=[4.0, 0.0]))
        self.assertEqual([r.duration for r in result], ["0:04", "0:00"])

    def test_duration_hours(self):
        m = message()
        self.assertEqual(rows(state(messages=[m], lengths=[3725.0]))[0].duration, "1:02:05")


class SessionRowsTest(unittest.TestCase):
    SESSIONS = [Session("1dd2b064-b8fd-4c1e-9a77-000000000001", "TTSReader"),
                Session("abcdef12-0000-4000-8000-000000000002", "")]

    def test_titles_ids_and_open(self):
        result = session_rows(state(sessions=self.SESSIONS, active=self.SESSIONS[0].id))
        self.assertEqual(result, [
            SessionRow("TTSReader · 1dd2b064", True, self.SESSIONS[0].id),
            SessionRow("— · abcdef12", False, self.SESSIONS[1].id)])
        self.assertTrue(clear_enabled(
            state(sessions=self.SESSIONS, active=self.SESSIONS[0].id)))

    def test_none_open(self):
        current = state(sessions=self.SESSIONS)
        self.assertEqual([r.open for r in session_rows(current)], [False, False])
        self.assertFalse(clear_enabled(current))

    def test_empty(self):
        self.assertEqual(session_rows(state()), [])


class TreeRowsTest(unittest.TestCase):
    SESSIONS = SessionRowsTest.SESSIONS

    def test_messages_follow_open_session_header(self):
        a, b = message("a", received_at=CREATED_AT), message("b", received_at=CREATED_AT)
        current = state("playing", b, messages=[a, b], lengths=[1.0, 2.0],
                        sessions=self.SESSIONS, active=self.SESSIONS[1].id)
        result = tree_rows(current)
        self.assertEqual([type(r) for r in result], [SessionRow, SessionRow, MessageRow, MessageRow])
        self.assertEqual(result[1:], [session_rows(current)[1], *rows(current)])

    def test_all_closed_shows_only_headers(self):
        current = state(sessions=self.SESSIONS)
        self.assertEqual(tree_rows(current), session_rows(current))

    def test_empty(self):
        self.assertEqual(tree_rows(state()), [])


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

    def test_play_pause_enabled_idle_with_any_message(self):
        """Без текущего play/pause играет последнее — прослушанное тоже."""
        heard, unheard = message("a", heard=True), message("b")
        self.assertTrue(play_pause_enabled(state("idle", messages=[heard, unheard])))
        self.assertTrue(play_pause_enabled(state("idle", messages=[heard])))

    def test_stop_enabled_only_with_current(self):
        """⏹ снимает курсор: есть текущее — в любом статусе, доигранное в idle тоже."""
        m = message(heard=True)
        for status in ("playing", "paused", "stopped", "idle"):
            self.assertTrue(stop_enabled(state(status, m, messages=[m])), status)
        self.assertFalse(stop_enabled(state("idle", messages=[m])))


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
