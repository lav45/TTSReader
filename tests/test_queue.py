"""Тесты MessageQueue: группировка в сообщения, порядок, навигация, хвост начатых (tail, по умолчанию 5)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tts_player.model import Message, Sentence
from tts_player.queue import MessageQueue


class MessageQueueTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.queue = MessageQueue()

    def sentence(self, recv_ns, token="t1", index=0, session="s1"):
        path = self.dir / f"{session}-{token}-{index}-{recv_ns}.wav"
        path.write_bytes(b"wav")
        return Sentence(recv_ns, session, token, index, path)

    def tokens(self):
        return [m.token for m in self.queue.messages]

    def test_same_session_and_token_make_one_message(self):
        a, b = self.sentence(1, index=0), self.sentence(2, index=1)
        self.queue.add(a)
        self.queue.add(b)
        self.assertEqual(1, len(self.queue.messages))
        self.assertEqual([a, b], self.queue.messages[0].sentences)

    def test_other_token_makes_new_message(self):
        self.queue.add(self.sentence(1, token="t1"))
        self.queue.add(self.sentence(2, token="t2"))
        self.assertEqual(["t1", "t2"], self.tokens())

    def test_other_session_same_token_is_other_message(self):
        a = self.sentence(1, session="s1")
        b = self.sentence(2, session="s2")
        self.queue.add(a)
        self.queue.add(b)
        self.assertEqual([[a], [b]], [m.sentences for m in self.queue.messages])

    def test_sentences_ordered_by_recv_ns(self):
        a, b, c = (self.sentence(n, index=n) for n in (1, 2, 3))
        for s in (c, a, b):
            self.queue.add(s)
        self.assertEqual([a, b, c], self.queue.messages[0].sentences)

    def test_messages_ordered_by_first_recv_ns_in_any_add_order(self):
        a = self.sentence(1, token="t1")
        b = self.sentence(2, token="t2")
        c = self.sentence(3, token="t3")
        for s in (c, a, b):
            self.queue.add(s)
        self.assertEqual(["t1", "t2", "t3"], self.tokens())

    def test_late_earlier_sentence_reorders_messages(self):
        self.queue.add(self.sentence(5, token="t1", index=1))
        self.queue.add(self.sentence(3, token="t2"))
        self.queue.add(self.sentence(1, token="t1", index=0))
        self.assertEqual(["t1", "t2"], self.tokens())

    def test_messages_is_a_copy(self):
        self.queue.add(self.sentence(1))
        self.queue.messages.clear()
        self.assertEqual(1, len(self.queue.messages))

    def test_empty_queue(self):
        self.assertEqual([], self.queue.messages)
        self.assertIsNone(self.queue.first_unheard())

    def test_following_and_preceding_walk_messages(self):
        for n in (1, 2, 3):
            self.queue.add(self.sentence(n, token=f"t{n}"))
        first, second, third = self.queue.messages
        self.assertIs(second, self.queue.following(first))
        self.assertIs(third, self.queue.following(second))
        self.assertIsNone(self.queue.following(third))
        self.assertIs(second, self.queue.preceding(third))
        self.assertIs(first, self.queue.preceding(second))
        self.assertIsNone(self.queue.preceding(first))

    def test_first_unheard_is_earliest_not_heard(self):
        for n in (1, 2, 3):
            self.queue.add(self.sentence(n, token=f"t{n}"))
        first, second, _ = self.queue.messages
        self.assertIs(first, self.queue.first_unheard())
        self.queue.mark_heard(first)
        self.assertTrue(first.heard)
        self.assertIs(second, self.queue.first_unheard())

    def test_first_unheard_none_when_all_heard(self):
        self.queue.add(self.sentence(1))
        self.queue.mark_heard(self.queue.messages[0])
        self.assertIsNone(self.queue.first_unheard())

    def test_new_message_is_not_heard(self):
        self.queue.add(self.sentence(1))
        self.assertFalse(self.queue.messages[0].heard)

    def test_is_complete_false_for_last_message_without_audio(self):
        self.queue.add(self.sentence(1))
        self.assertFalse(self.queue.is_complete(self.queue.messages[0]))

    def test_is_complete_true_when_later_message_appears(self):
        self.queue.add(self.sentence(1, token="t1"))
        first = self.queue.messages[0]
        self.queue.add(self.sentence(2, token="t2"))
        self.assertTrue(self.queue.is_complete(first))
        self.assertFalse(self.queue.is_complete(self.queue.messages[1]))

    def test_late_sentence_of_started_message_is_added(self):
        a, b = self.sentence(1, index=0), self.sentence(2, index=1)
        self.queue.add(a)
        message = self.queue.messages[0]
        self.queue.start(message)
        self.queue.add(b)
        self.assertEqual([a, b], message.sentences)

    def test_sentence_of_heard_message_is_deleted_not_added(self):
        self.queue.add(self.sentence(1, token="t1", index=0))
        message = self.queue.messages[0]
        self.queue.start(message)
        self.queue.mark_heard(message)
        late = self.sentence(3, token="t1", index=1)
        with self.assertLogs("tts_player.queue", "INFO"):
            self.queue.add(late)
        self.assertEqual(1, len(message.sentences))
        self.assertFalse(late.path.exists())

    def start_messages(self, count):
        sentences = [self.sentence(n, token=f"t{n}") for n in range(1, count + 1)]
        for s in sentences:
            self.queue.add(s)
        for message in self.queue.messages:
            self.queue.start(message)
        return sentences

    def test_sixth_started_message_deletes_first_only(self):
        sentences = [self.sentence(n, token=f"t{n}") for n in range(1, 7)]
        for s in sentences:
            self.queue.add(s)
        messages = self.queue.messages
        for message in messages[:5]:
            self.queue.start(message)
        self.assertTrue(all(s.path.exists() for s in sentences))
        self.queue.start(messages[5])
        self.assertFalse(sentences[0].path.exists())
        self.assertTrue(all(s.path.exists() for s in sentences[1:]))
        self.assertEqual(messages[1:], self.queue.messages)

    def start_token(self, token):
        self.queue.start(next(m for m in self.queue.messages if m.token == token))

    def test_default_tail_is_five(self):
        self.assertEqual(5, self.queue.tail)

    def test_smaller_tail_shrinks_by_one_per_start(self):
        """tail 5 → 2 при пяти начатых: каждый start уменьшает хвост на одно сообщение,
        а не обрезает сразу до двух; через три новых начатых их 2."""
        self.start_messages(5)
        self.queue.tail = 2
        for n in range(6, 9):
            self.queue.add(self.sentence(n, token=f"t{n}"))
        self.start_token("t6")
        self.assertEqual(["t3", "t4", "t5", "t6", "t7", "t8"], self.tokens())
        self.start_token("t7")
        self.assertEqual(["t5", "t6", "t7", "t8"], self.tokens())
        self.start_token("t8")
        self.assertEqual(["t7", "t8"], self.tokens())

    def test_shrunk_tail_then_holds_its_size(self):
        self.start_messages(5)
        self.queue.tail = 2
        for n in range(6, 10):
            self.queue.add(self.sentence(n, token=f"t{n}"))
            self.queue.start(self.queue.messages[-1])
        self.assertEqual(["t8", "t9"], self.tokens())

    def test_larger_tail_stops_evicting_until_new_limit(self):
        """tail 2 → 5: новые начатые не вытесняют, пока их меньше 5."""
        self.queue.tail = 2
        self.start_messages(2)
        self.queue.tail = 5
        for n in range(3, 6):
            self.queue.add(self.sentence(n, token=f"t{n}"))
            self.queue.start(self.queue.messages[-1])
        self.assertEqual(["t1", "t2", "t3", "t4", "t5"], self.tokens())
        self.queue.add(self.sentence(6, token="t6"))
        self.queue.start(self.queue.messages[-1])
        self.assertEqual(["t2", "t3", "t4", "t5", "t6"], self.tokens())

    def test_restarting_started_message_does_not_evict(self):
        sentences = self.start_messages(5)
        self.queue.start(self.queue.messages[0])
        self.assertTrue(all(s.path.exists() for s in sentences))

    def test_deleted_message_leaves_queue_and_missing_file_is_fine(self):
        sentences = [self.sentence(n, token=f"t{n}") for n in range(1, 8)]
        for s in sentences:
            self.queue.add(s)
        sentences[1].path.unlink()
        for message in self.queue.messages:
            self.queue.start(message)
        self.assertFalse(any(s.path.exists() for s in sentences[:2]))
        self.assertTrue(all(s.path.exists() for s in sentences[2:]))
        self.assertEqual(5, len(self.queue.messages))

    def test_all_sentences_of_oldest_message_deleted(self):
        first = [self.sentence(1, index=0), self.sentence(2, index=1)]
        for s in first:
            self.queue.add(s)
        for n in range(3, 8):
            self.queue.add(self.sentence(n, token=f"t{n}"))
        for message in self.queue.messages:
            self.queue.start(message)
        self.assertFalse(any(s.path.exists() for s in first))

    def test_unstarted_messages_never_deleted(self):
        sentences = [self.sentence(n, token=f"t{n}") for n in range(1, 21)]
        for s in sentences:
            self.queue.add(s)
        self.queue.start(self.queue.messages[0])
        self.assertTrue(all(s.path.exists() for s in sentences))
        self.assertEqual(20, len(self.queue.messages))

    def test_unlink_failure_on_eviction_does_not_break_queue(self):
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("denied")):
            with self.assertLogs("tts_player.queue", "ERROR"):
                self.start_messages(7)
        self.assertEqual(["t3", "t4", "t5", "t6", "t7"], self.tokens())

    def test_sentence_of_evicted_message_is_deleted_not_added(self):
        self.start_messages(6)
        late = self.sentence(100, token="t1", index=1)
        with self.assertLogs("tts_player.queue", "INFO"):
            self.queue.add(late)
        self.assertFalse(late.path.exists())
        self.assertNotIn("t1", self.tokens())

    def test_sentence_of_evicted_message_with_missing_file_is_fine(self):
        self.start_messages(6)
        late = self.sentence(100, token="t1", index=1)
        late.path.unlink()
        self.queue.add(late)
        self.assertNotIn("t1", self.tokens())

    def test_sentence_of_evicted_message_unlink_error_is_logged(self):
        self.start_messages(6)
        late = self.sentence(100, token="t1", index=1)
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("denied")):
            with self.assertLogs("tts_player.queue", "ERROR") as logs:
                self.queue.add(late)
        self.assertTrue(any(str(late.path) in line for line in logs.output))
        self.assertNotIn("t1", self.tokens())

    def test_messages_with_same_fields_are_different_messages(self):
        self.assertNotEqual(Message("s1", "t1", []), Message("s1", "t1", []))

    def test_release_returns_unheard_and_keeps_their_files(self):
        a0, a1 = self.sentence(1, token="ta", index=0), self.sentence(2, token="ta", index=1)
        b0 = self.sentence(3, token="tb")
        for s in (b0, a1, a0):
            self.queue.add(s)
        self.assertEqual([a0, a1, b0], self.queue.release(None, 0.0))
        self.assertTrue(all(s.path.exists() for s in (a0, a1, b0)))

    def test_release_deletes_heard(self):
        a0, b0 = self.sentence(1, token="ta"), self.sentence(2, token="tb")
        self.queue.add(a0)
        self.queue.add(b0)
        first, second = self.queue.messages
        self.queue.start(first)
        self.queue.mark_heard(first)
        self.queue.start(second)
        with self.assertLogs("tts_player.queue", "WARNING"):
            released = self.queue.release(second, 0.0)
        self.assertEqual([b0], released)
        self.assertFalse(a0.path.exists())
        self.assertTrue(b0.path.exists())

    def test_release_when_all_heard_returns_nothing_and_deletes_all(self):
        a0 = self.sentence(1, token="ta")
        self.queue.add(a0)
        self.queue.mark_heard(self.queue.messages[0])
        self.assertEqual([], self.queue.release(None, 0.0))
        self.assertFalse(a0.path.exists())

    def test_release_forgets_all_messages(self):
        self.queue.add(self.sentence(1, token="ta"))
        self.queue.add(self.sentence(2, token="tb"))
        self.queue.release(None, 0.0)
        self.assertEqual([], self.queue.messages)
        self.assertEqual([], self.queue.release(None, 0.0))

    def test_clear_deletes_all_files_and_forgets(self):
        sentences = [self.sentence(n, token=f"t{n}") for n in range(1, 4)]
        for s in sentences:
            self.queue.add(s)
        self.queue.start(self.queue.messages[0])
        self.queue.mark_heard(self.queue.messages[0])
        self.queue.clear()
        self.assertEqual([], self.queue.messages)
        self.assertFalse(any(s.path.exists() for s in sentences))
        self.assertEqual([], self.queue.release(None, 0.0))

    def test_sentence_of_cleared_message_is_deleted_not_added(self):
        self.queue.add(self.sentence(1, token="t1", index=0))
        self.queue.clear()
        late = self.sentence(2, token="t1", index=1)
        with self.assertLogs("tts_player.queue", "INFO"):
            self.queue.add(late)
        self.assertFalse(late.path.exists())
        self.assertEqual([], self.queue.messages)

    def test_new_message_after_clear_is_added(self):
        self.queue.add(self.sentence(1, token="t1"))
        self.queue.clear()
        fresh = self.sentence(2, token="t2")
        self.queue.add(fresh)
        self.assertEqual([[fresh]], [m.sentences for m in self.queue.messages])

    def test_release_forgets_evicted_keys(self):
        self.start_messages(6)
        self.queue.release(None, 0.0)
        late = self.sentence(100, token="t1", index=1)
        self.queue.add(late)
        self.assertEqual(["t1"], self.tokens())

    def test_start_of_forgotten_message_is_noop(self):
        self.queue.add(self.sentence(1, token="t1"))
        message = self.queue.messages[0]
        self.queue.clear()
        with self.assertLogs("tts_player.queue", "DEBUG"):
            self.queue.start(message)
        self.assertEqual([], self.queue.messages)
        self.start_messages(6)
        self.assertEqual(5, len(self.queue.messages))


if __name__ == "__main__":
    unittest.main()
