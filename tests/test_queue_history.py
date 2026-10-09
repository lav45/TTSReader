"""Тесты привязки склейки демона к сообщению очереди, длины и карты позиций."""
import logging
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from tts_player.history import HistoryEntry
from tts_player.model import Message, Sentence
from tts_player.queue import MessageQueue

RATE = 1000


def write_wav(path: Path, seconds: float) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"\0\0" * int(seconds * RATE))


class QueueHistoryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.dir = root / "sentences"
        self.store = root / "store"
        self.history = root / "history"
        for d in (self.dir, self.store, self.history):
            d.mkdir()
        self.queue = MessageQueue(tail=5)

    def add(self, recv_ns, seconds, task="t1", index=0, session="s1", token="aa"):
        """Предложение задачи task; сообщения для task ещё нет — оно заводится первым."""
        if not any(task in m.task_ids for m in self.queue.messages):
            message = Message(session)
            self.queue.append(message)
            self.queue.link(message, task)
        path = self.dir / f"{task}-{session}-{token}-{index}-{recv_ns}.wav"
        write_wav(path, seconds)
        sentence = Sentence(recv_ns, task, session, token, index, path)
        self.queue.add(sentence)
        return sentence

    def entry(self, duration, created_s=100.0, session="s1", name="tts-1.wav"):
        path = self.history / name
        write_wav(path, duration)
        return HistoryEntry(path, created_s, session, "proj", duration)

    def message(self, task="t1"):
        return next(m for m in self.queue.messages if task in m.task_ids)

    def test_clip_of_second_pass_ends_message_of_both_passes(self):
        """Как у демона: пауза обрывает проход 1 на s1 без записи истории, проход 2 (новый
        token) начинает с того же s1, склейка — только из предложений прохода 2. Длительность
        не сходится с сообщением обоих проходов: audio нет, но сообщение завершено."""
        self.add(1_000_000_000, 1, token="aa", index=0, task="f00d")
        self.add(2_000_000_000, 1, token="aa", index=1, task="f00d")
        self.add(3_000_000_000, 1, token="bb", index=1, task="f00d")
        self.add(4_000_000_000, 1, token="bb", index=2, task="f00d")
        with self.assertLogs("tts_player.queue", "WARNING"):
            message = self.queue.attach(self.entry(2), self.store)
        self.assertEqual([message], self.queue.messages)
        self.assertEqual(4, len(message.sentences))
        self.assertIsNone(message.audio)
        self.assertEqual(100.0, message.created_at)
        self.assertTrue(self.queue.is_complete(message))

    def test_attaches_latest_message_of_session_without_audio(self):
        self.add(1_000_000_000, 1, task="old")
        self.add(2_000_000_000, 1, task="new")
        self.add(3_000_000_000, 1, task="x", session="s2")
        message = self.queue.attach(self.entry(1), self.store)
        self.assertIs(self.message("new"), message)
        self.assertIsNone(self.message("old").audio)
        self.assertIsNone(self.message("x").audio)

    def test_message_without_sentences_is_not_candidate(self):
        """Сообщение задачи хука, чьих предложений ещё нет, склейку не берёт."""
        with_sentences = self.message_with(1_000_000_000, "old")
        empty = Message("s1")
        self.queue.append(empty)
        self.queue.link(empty, "new")
        self.assertIs(with_sentences, self.queue.attach(self.entry(1), self.store))
        self.assertIsNone(empty.created_at)

    def message_with(self, recv_ns, task):
        self.add(recv_ns, 1, task=task)
        return self.message(task)

    def test_matching_copies_audio_and_fills_fields(self):
        self.add(1_000_000_000, 1.0, index=0)
        self.add(2_000_000_000, 0.5, index=1)
        entry = self.entry(1.5)
        message = self.queue.attach(entry, self.store)
        self.assertEqual(self.store / "tts-1.wav", message.audio)
        self.assertTrue(message.audio.exists())
        self.assertTrue(entry.path.exists())
        self.assertEqual((100.0, "proj", 1.5),
                         (message.created_at, message.project, message.duration_s))

    def test_mismatch_leaves_audio_none_with_warning(self):
        self.add(1_000_000_000, 1.0)
        with self.assertLogs("tts_player.queue", logging.WARNING):
            message = self.queue.attach(self.entry(1.2), self.store)
        self.assertIsNone(message.audio)
        self.assertEqual([], list(self.store.iterdir()))
        self.assertEqual((100.0, "proj"), (message.created_at, message.project))

    def test_tolerance_is_inclusive(self):
        self.add(1_000_000_000, 1.0)
        message = self.queue.attach(self.entry(1.05), self.store)
        self.assertIsNotNone(message.audio)

    def test_no_candidate_returns_none_and_copies_nothing(self):
        self.add(1_000_000_000, 1.0, session="s2")
        self.assertIsNone(self.queue.attach(self.entry(1.0), self.store))
        self.assertEqual([], list(self.store.iterdir()))

    def test_sentence_received_after_entry_is_not_candidate(self):
        self.add(101_000_000_000, 1.0)
        self.assertIsNone(self.queue.attach(self.entry(1.0), self.store))

    def test_already_attached_message_is_skipped(self):
        self.add(1_000_000_000, 1.0, task="a")
        self.add(2_000_000_000, 1.0, task="b")
        first = self.queue.attach(self.entry(1.0, name="tts-1.wav"), self.store)
        second = self.queue.attach(self.entry(1.0, name="tts-2.wav"), self.store)
        self.assertEqual((self.message("b"), self.message("a")), (first, second))

    def test_copy_failure_is_mismatch(self):
        self.add(1_000_000_000, 1.0)
        with mock.patch("tts_player.queue.shutil.copy2", side_effect=OSError("x")):
            with self.assertLogs("tts_player.queue", logging.WARNING):
                message = self.queue.attach(self.entry(1.0), self.store)
        self.assertIsNone(message.audio)

    def test_unreadable_sentence_counts_zero_with_warning(self):
        sentence = self.add(1_000_000_000, 1.0)
        sentence.path.write_bytes(b"garbage")
        with self.assertLogs("tts_player.queue", logging.WARNING):
            offset = self.queue.offset_of(self.message(), sentence)
            self.assertEqual(0, offset)
            self.assertIsNone(self.queue.attach(self.entry(1.0), self.store).audio)

    def test_locate_before_audio_maps_into_sentence(self):
        self.add(1_000_000_000, 1.0, index=0)
        second = self.add(2_000_000_000, 2.0, index=1)
        message = self.message()
        path, offset = self.queue.locate(message, 1.5)
        self.assertEqual(second.path, path)
        self.assertAlmostEqual(0.5, offset)
        self.assertAlmostEqual(1.0, self.queue.offset_of(message, second))

    def test_locate_clamps(self):
        first = self.add(1_000_000_000, 1.0, index=0)
        second = self.add(2_000_000_000, 2.0, index=1)
        message = self.message()
        self.assertEqual((first.path, 0), self.queue.locate(message, -3))
        path, offset = self.queue.locate(message, 99)
        self.assertEqual(second.path, path)
        self.assertAlmostEqual(2.0, offset)

    def test_locate_finds_zero_length_sentence_at_position(self):
        first = self.add(1_000_000_000, 0, index=0)
        self.add(2_000_000_000, 1.0, index=1)
        middle = self.add(3_000_000_000, 0, index=2)
        message = self.message()
        self.assertEqual((first.path, 0), self.queue.locate(message, 0))
        self.assertEqual((middle.path, 0), self.queue.locate(message, 1.0))

    def test_locate_after_audio(self):
        self.add(1_000_000_000, 1.0)
        message = self.queue.attach(self.entry(1.0), self.store)
        self.assertEqual((message.audio, 0.4), self.queue.locate(message, 0.4))

    def test_duration_read_once(self):
        self.add(1_000_000_000, 1.0)
        message = self.message()
        self.queue.locate(message, 0.5)
        with mock.patch("tts_player.queue.wave.open", side_effect=AssertionError):
            self.queue.locate(message, 0.5)

    def test_length_before_audio_is_sum_of_received(self):
        self.add(1_000_000_000, 1.0, index=0)
        message = self.message()
        self.assertAlmostEqual(1.0, self.queue.length(message))
        self.add(2_000_000_000, 0.5, index=1)
        self.assertAlmostEqual(1.5, self.queue.length(message))

    def test_length_after_audio_is_duration(self):
        self.add(1_000_000_000, 1.0)
        message = self.queue.attach(self.entry(1.04), self.store)
        self.assertEqual(1.04, self.queue.length(message))

    def test_length_after_mismatch_is_sum_of_received(self):
        self.add(1_000_000_000, 1.0)
        with self.assertLogs("tts_player.queue", logging.WARNING):
            message = self.queue.attach(self.entry(5.0), self.store)
        self.assertAlmostEqual(1.0, self.queue.length(message))

    def test_is_complete_after_attach(self):
        self.add(1_000_000_000, 1.0)
        message = self.message()
        self.assertFalse(self.queue.is_complete(message))
        self.queue.attach(self.entry(1.0), self.store)
        self.assertTrue(self.queue.is_complete(message))

    def test_is_complete_after_mismatched_attach(self):
        self.add(1_000_000_000, 1.0)
        with self.assertLogs("tts_player.queue", logging.WARNING):
            message = self.queue.attach(self.entry(5.0), self.store)
        self.assertTrue(self.queue.is_complete(message))

    def attach_first(self, task, recv_ns, name="tts-1.wav"):
        self.add(recv_ns, 1.0, task=task)
        message = self.queue.attach(self.entry(1.0, name=name), self.store)
        self.assertIsNotNone(message.audio)
        return message

    def test_unstarted_audio_message_not_evicted(self):
        for i in range(self.queue.tail + 1):
            self.add((i + 1) * 1_000_000_000, 0.1, task=f"m{i}")
        for message in self.queue.messages:
            self.queue.start(message)
        message = self.attach_first("last", 50_000_000_000, name="tts-9.wav")
        self.assertIn(message, self.queue.messages)
        self.assertTrue(message.audio.exists())

    def audio_message(self):
        """Сообщение из двух предложений по 0,5 с со склейкой 1,0 с."""
        a = self.add(1_000_000_000, 0.5, task="a", index=0)
        b = self.add(2_000_000_000, 0.5, task="a", index=1)
        message = self.queue.attach(self.entry(1.0), self.store)
        self.assertIsNotNone(message.audio)
        return message, [a, b]

    def test_failed_copy_leaves_no_partial_file(self):
        self.add(1_000_000_000, 1.0)

        def partial_copy(src, dst):
            Path(dst).write_bytes(b"part")
            raise OSError("диск полон")

        with mock.patch("tts_player.queue.shutil.copy2", side_effect=partial_copy):
            with self.assertLogs("tts_player.queue", logging.WARNING):
                message = self.queue.attach(self.entry(1.0), self.store)
        self.assertIsNone(message.audio)
        self.assertEqual([], list(self.store.iterdir()))

    def test_eviction_deletes_audio(self):
        self.add(1_000_000_000, 1.0, task="m0")
        message = self.queue.attach(self.entry(1.0), self.store)
        for i in range(1, self.queue.tail + 1):
            self.add((i + 1) * 1_000_000_000, 0.1, task=f"m{i}")
        for item in self.queue.messages:
            self.queue.start(item)
        self.assertFalse(message.audio.exists())
        self.assertNotIn(message, self.queue.messages)

    def test_clear_deletes_audio_and_sentences(self):
        message, sentences = self.audio_message()
        self.queue.clear()
        self.assertEqual([], list(self.store.iterdir()))
        self.assertFalse(any(s.path.exists() for s in sentences))
        self.assertEqual([], self.queue.messages)


if __name__ == "__main__":
    unittest.main()
