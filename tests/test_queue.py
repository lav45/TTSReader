"""Тесты MessageQueue: группировка в сообщения, порядок выдачи, хвост из 5 сообщений."""
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

    def drain(self):
        result = []
        while (sentence := self.queue.next_item()) is not None:
            result.append(sentence)
        return result

    def test_same_session_and_token_make_one_message(self):
        a, b = self.sentence(1, index=0), self.sentence(2, index=1)
        self.queue.add(a)
        self.queue.add(b)
        self.queue.next_item()
        self.assertEqual([a, b], self.queue.current.sentences)

    def test_other_token_makes_new_message(self):
        a, b = self.sentence(1, token="t1"), self.sentence(2, token="t2")
        self.queue.add(a)
        self.queue.add(b)
        self.assertEqual(a, self.queue.next_item())
        self.assertEqual("t1", self.queue.current.token)
        self.assertEqual(b, self.queue.next_item())
        self.assertEqual("t2", self.queue.current.token)

    def test_other_session_same_token_is_other_message(self):
        a = self.sentence(1, session="s1")
        b = self.sentence(2, session="s2")
        self.queue.add(a)
        self.queue.add(b)
        self.queue.next_item()
        self.assertEqual([a], self.queue.current.sentences)

    def test_sentences_by_recv_ns_each_once(self):
        a, b, c = (self.sentence(n, index=n) for n in (1, 2, 3))
        for s in (c, a, b):
            self.queue.add(s)
        self.assertEqual([a, b, c], self.drain())
        self.assertIsNone(self.queue.next_item())

    def test_empty_queue(self):
        self.assertIsNone(self.queue.next_item())
        self.assertIsNone(self.queue.current)

    def test_late_sentence_of_current_message_is_next(self):
        a, b = self.sentence(1, index=0), self.sentence(2, index=1)
        self.queue.add(a)
        self.assertEqual(a, self.queue.next_item())
        self.assertIsNone(self.queue.next_item())
        self.queue.add(b)
        self.assertEqual(b, self.queue.next_item())

    def test_late_sentence_with_earlier_recv_ns_is_handed_once_after(self):
        a, b, early = self.sentence(5, index=1), self.sentence(6, index=2), self.sentence(1, index=0)
        self.queue.add(a)
        self.assertEqual(a, self.queue.next_item())
        self.queue.add(early)
        self.queue.add(b)
        self.assertEqual([early, b], self.drain())

    def test_moves_to_later_message_when_current_exhausted(self):
        a, b = self.sentence(1, token="t1"), self.sentence(2, token="t2")
        self.queue.add(a)
        self.assertEqual(a, self.queue.next_item())
        self.queue.add(b)
        self.assertEqual(b, self.queue.next_item())
        self.assertEqual("t2", self.queue.current.token)

    def test_messages_ordered_by_first_recv_ns(self):
        a = self.sentence(1, token="t1")
        b = self.sentence(2, token="t2")
        self.queue.add(b)
        self.queue.add(a)
        self.assertEqual([a, b], self.drain())

    def test_sentence_of_finished_message_is_not_played(self):
        a, b = self.sentence(1, token="t1", index=0), self.sentence(2, token="t2")
        late = self.sentence(3, token="t1", index=1)
        self.queue.add(a)
        self.queue.add(b)
        self.drain()
        self.queue.add(late)
        self.assertIsNone(self.queue.next_item())
        self.assertTrue(late.path.exists())

    def play_messages(self, count):
        sentences = [self.sentence(n, token=f"t{n}") for n in range(1, count + 1)]
        for s in sentences:
            self.queue.add(s)
        return sentences

    def test_sixth_message_deletes_first_only(self):
        sentences = self.play_messages(6)
        for s in sentences[:5]:
            self.assertEqual(s, self.queue.next_item())
        self.assertTrue(all(s.path.exists() for s in sentences))
        self.assertEqual(sentences[5], self.queue.next_item())
        self.assertFalse(sentences[0].path.exists())
        self.assertTrue(all(s.path.exists() for s in sentences[1:]))

    def test_deleted_message_leaves_queue_and_missing_file_is_fine(self):
        sentences = self.play_messages(7)
        sentences[1].path.unlink()
        self.drain()
        self.assertFalse(any(s.path.exists() for s in sentences[:2]))
        self.assertTrue(all(s.path.exists() for s in sentences[2:]))

    def test_all_sentences_of_oldest_message_deleted(self):
        first = [self.sentence(1, index=0), self.sentence(2, index=1)]
        for s in first:
            self.queue.add(s)
        rest = [self.sentence(n, token=f"t{n}") for n in range(3, 8)]
        for s in rest:
            self.queue.add(s)
        self.drain()
        self.assertFalse(any(s.path.exists() for s in first))

    def test_unplayed_messages_never_deleted(self):
        sentences = self.play_messages(20)
        self.queue.next_item()
        self.assertTrue(all(s.path.exists() for s in sentences))

    def test_unlink_failure_on_eviction_does_not_break_queue(self):
        sentences = self.play_messages(7)
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("denied")):
            handed = self.drain()
        self.assertEqual(sentences, handed)

    def test_sentence_of_evicted_message_is_deleted_not_played(self):
        sentences = self.play_messages(6)
        self.drain()
        late = self.sentence(100, token="t1", index=1)
        self.queue.add(late)
        self.assertIsNone(self.queue.next_item())
        self.assertFalse(late.path.exists())

    def test_sentence_of_evicted_message_with_missing_file_is_fine(self):
        self.play_messages(6)
        self.drain()
        late = self.sentence(100, token="t1", index=1)
        late.path.unlink()
        self.queue.add(late)
        self.assertIsNone(self.queue.next_item())

    def test_sentence_of_evicted_message_unlink_error_is_logged(self):
        self.play_messages(6)
        self.drain()
        late = self.sentence(100, token="t1", index=1)
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("denied")):
            with self.assertLogs("tts_player.queue", "ERROR") as logs:
                self.queue.add(late)
        self.assertTrue(any(str(late.path) in line for line in logs.output))
        self.assertIsNone(self.queue.next_item())

    def test_messages_with_same_fields_are_different_messages(self):
        self.assertNotEqual(Message("s1", "t1", []), Message("s1", "t1", []))

    def test_release_returns_playing_and_unplayed_files_kept(self):
        a0, a1 = self.sentence(1, token="ta", index=0), self.sentence(2, token="ta", index=1)
        b0 = self.sentence(3, token="tb")
        for s in (b0, a1, a0):
            self.queue.add(s)
        self.assertEqual(a0, self.queue.next_item())
        self.assertEqual([a0, a1, b0], self.queue.release())
        self.assertTrue(all(s.path.exists() for s in (a0, a1, b0)))

    def test_release_deletes_finished(self):
        a0, b0 = self.sentence(1, token="ta"), self.sentence(2, token="tb")
        self.queue.add(a0)
        self.queue.add(b0)
        self.queue.next_item()
        self.queue.next_item()
        self.assertEqual([b0], self.queue.release())
        self.assertFalse(a0.path.exists())
        self.assertTrue(b0.path.exists())

    def test_release_when_idle_returns_nothing_and_deletes_all(self):
        a0 = self.sentence(1, token="ta")
        self.queue.add(a0)
        self.drain()
        self.assertEqual([], self.queue.release())
        self.assertFalse(a0.path.exists())

    def test_release_deletes_late_sentence_of_finished_message(self):
        a0, b0 = self.sentence(1, token="ta", index=0), self.sentence(2, token="tb")
        self.queue.add(a0)
        self.queue.add(b0)
        self.drain()
        late = self.sentence(3, token="ta", index=1)
        self.queue.add(late)
        self.assertEqual([], self.queue.release())
        self.assertFalse(late.path.exists())

    def test_release_forgets_all_messages(self):
        a0, b0 = self.sentence(1, token="ta"), self.sentence(2, token="tb")
        self.queue.add(a0)
        self.queue.add(b0)
        self.queue.next_item()
        self.queue.release()
        self.assertIsNone(self.queue.current)
        self.assertIsNone(self.queue.next_item())
        self.assertEqual([], self.queue.release())


if __name__ == "__main__":
    unittest.main()
