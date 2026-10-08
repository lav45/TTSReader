"""Тесты привязки склейки демона к сообщению очереди и карты позиций."""
import logging
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from tts_player.history import HistoryEntry
from tts_player.model import Sentence
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
        self.queue = MessageQueue()

    def add(self, recv_ns, seconds, token="t1", index=0, session="s1"):
        path = self.dir / f"{session}-{token}-{index}-{recv_ns}.wav"
        write_wav(path, seconds)
        sentence = Sentence(recv_ns, session, token, index, path)
        self.queue.add(sentence)
        return sentence

    def entry(self, duration, created_s=100.0, session="s1", name="tts-1.wav"):
        path = self.history / name
        write_wav(path, duration)
        return HistoryEntry(path, created_s, session, "proj", duration)

    def message(self, token="t1", session="s1"):
        return next(m for m in self.queue._messages
                    if m.token == token and m.session_id == session)

    def test_attaches_latest_message_of_session_without_audio(self):
        self.add(1_000_000_000, 1, token="old")
        self.add(2_000_000_000, 1, token="new")
        other = self.add(3_000_000_000, 1, token="x", session="s2")
        message = self.queue.attach(self.entry(1), self.store)
        self.assertEqual("new", message.token)
        self.assertIsNone(self.message("old").audio)
        self.assertIsNone(self.queue._find("s2", other.token).audio)

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
        self.add(1_000_000_000, 1.0, token="a")
        self.add(2_000_000_000, 1.0, token="b")
        first = self.queue.attach(self.entry(1.0, name="tts-1.wav"), self.store)
        second = self.queue.attach(self.entry(1.0, name="tts-2.wav"), self.store)
        self.assertEqual(("b", "a"), (first.token, second.token))

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

    def test_attached_current_does_not_hand_out_rest(self):
        first = self.add(1_000_000_000, 1.0, index=0)
        self.add(2_000_000_000, 1.0, index=1)
        self.assertEqual(first, self.queue.next_item())
        self.queue.attach(self.entry(2.0), self.store)
        self.assertIsNone(self.queue.next_item())

    def test_attached_current_with_mismatch_keeps_handing_out(self):
        first = self.add(1_000_000_000, 1.0, index=0)
        second = self.add(2_000_000_000, 1.0, index=1)
        self.queue.next_item()
        with self.assertLogs("tts_player.queue", logging.WARNING):
            self.queue.attach(self.entry(5.0), self.store)
        self.assertEqual(second, self.queue.next_item())

    def attach_first(self, token, recv_ns, name="tts-1.wav"):
        self.add(recv_ns, 1.0, token=token)
        message = self.queue.attach(self.entry(1.0, name=name), self.store)
        self.assertIsNotNone(message.audio)
        return message

    def test_unstarted_audio_message_is_returned_then_next_sentence(self):
        message = self.attach_first("a", 1_000_000_000)
        plain = self.add(2_000_000_000, 1.0, token="b")
        self.assertIs(message, self.queue.next_item())
        self.assertEqual(plain, self.queue.next_item())

    def test_audio_message_alone_returns_message_then_none(self):
        message = self.attach_first("a", 1_000_000_000)
        self.assertIs(message, self.queue.next_item())
        self.assertIsNone(self.queue.next_item())

    def test_audio_message_not_evicted_before_returned(self):
        for i in range(MessageQueue.TAIL):
            self.add((i + 1) * 1_000_000_000, 0.1, token=f"m{i}")
            self.queue.next_item()
        message = self.attach_first("last", 50_000_000_000, name="tts-9.wav")
        self.assertIs(message, self.queue.next_item())
        self.assertTrue(message.audio.exists())

    def test_release_playing_audio_message_returns_all_sentences(self):
        a = self.add(1_000_000_000, 0.5, token="a", index=0)
        b = self.add(2_000_000_000, 0.5, token="a", index=1)
        message = self.queue.attach(self.entry(1.0), self.store)
        self.assertIs(message, self.queue.next_item())
        self.assertEqual([a, b], self.queue.release())
        self.assertFalse(message.audio.exists())
        self.assertTrue(a.path.exists() and b.path.exists())

    def test_release_current_audio_message_not_playing_returns_all_sentences(self):
        a = self.add(1_000_000_000, 0.5, token="a", index=0)
        b = self.add(2_000_000_000, 0.5, token="a", index=1)
        message = self.queue.attach(self.entry(1.0), self.store)
        self.queue.next_item()
        self.assertIsNone(self.queue.next_item())
        self.assertEqual([a, b], self.queue.release())
        self.assertFalse(message.audio.exists())

    def test_eviction_deletes_audio(self):
        self.add(1_000_000_000, 1.0, token="m0")
        message = self.queue.attach(self.entry(1.0), self.store)
        self.queue.next_item()
        for i in range(1, MessageQueue.TAIL + 1):
            self.add((i + 1) * 1_000_000_000, 0.1, token=f"m{i}")
            self.queue.next_item()
        self.assertFalse(message.audio.exists())

    def test_release_deletes_audio(self):
        self.add(1_000_000_000, 1.0)
        message = self.queue.attach(self.entry(1.0), self.store)
        self.queue.release()
        self.assertFalse(message.audio.exists())


if __name__ == "__main__":
    unittest.main()
