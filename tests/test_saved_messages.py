"""Тесты SavedMessages: снимок очереди в messages.json при выходе и обратно при старте."""
import json
import tempfile
import unittest
from pathlib import Path

from tts_player.model import Message, Sentence
from tts_player.queue import QueueSnapshot
from tts_player.saved_messages import SavedMessages

S0 = "1000000000000000001_aa_tts_queue_S_aa_s0.wav"
S1 = "1000000000000000002_aa_tts_queue_S_aa_s1.wav"
JOINED = "tts-1.wav"


class SavedMessagesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.store = root / "store"
        self.store.mkdir()
        self.path = root / "messages.json"
        self.saved = SavedMessages(self.path, self.store)

    def sentence(self, name: str, index: int) -> Sentence:
        path = self.store / name
        path.write_bytes(b"wav")
        return Sentence(int(name[:19]), "aa", "S", "aa", index, path)

    def snapshot(self) -> QueueSnapshot:
        joined = self.store / JOINED
        joined.write_bytes(b"wav")
        unheard = Message("S", project="/w/p", text="Привет", persona="p", speed=1.5,
                          task_ids={"aa", "bb"},
                          sentences=[self.sentence(S0, 0), self.sentence(S1, 1)],
                          received_at=5.0, audio=joined, created_at=7.0, duration_s=1.0)
        heard = Message("N", text="пока", persona="q", speed=1.0, task_ids={"cc"},
                        received_at=6.0, heard=True)
        not_sent = Message("S", text="x", sent=False)
        deferred = Message("B", text="отложено", persona="p", speed=1.0, received_at=8.0,
                           sent=False, deferred=True)
        return QueueSnapshot([unheard, heard, not_sent, deferred], [unheard],
                             {"aa": 1, "bb": 3, "cc": 2}, 3)

    def fields(self, message: Message) -> tuple:
        return (message.session_id, message.project, message.text, message.persona,
                message.speed, message.task_ids, message.sentences, message.received_at,
                message.sent, message.audio, message.created_at, message.duration_s,
                message.heard, message.deferred)

    def test_round_trip_and_file_deleted_after_load(self):
        snapshot = self.snapshot()
        self.saved.save(snapshot)
        self.assertTrue(self.path.exists())
        loaded = self.saved.load()
        self.assertFalse(self.path.exists())
        self.assertEqual([self.fields(m) for m in snapshot.messages],
                         [self.fields(m) for m in loaded.messages])
        self.assertEqual([loaded.messages[0]], loaded.started)
        self.assertEqual(({"aa": 1, "bb": 3, "cc": 2}, 3), (loaded.task_order, loaded.delivered))

    def test_deferred_is_saved_and_missing_key_reads_as_false(self):
        self.saved.save(self.snapshot())
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual([False, False, False, True], [m["deferred"] for m in data["messages"]])
        for item in data["messages"]:
            del item["deferred"]
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual([False] * 4, [m.deferred for m in self.saved.load().messages])

    def test_saved_file_names_are_relative_to_store(self):
        self.saved.save(self.snapshot())
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(([S0, S1], JOINED),
                         (data["messages"][0]["sentences"], data["messages"][0]["audio"]))

    def test_missing_files_are_dropped(self):
        """Пропавшее предложение выпадает; без склейки сообщение играет по предложениям,
        без всего — «без звука» и при play переозвучивается."""
        self.saved.save(self.snapshot())
        (self.store / S1).unlink()
        (self.store / JOINED).unlink()
        message = self.saved.load().messages[0]
        self.assertEqual(([S0], None, None),
                         ([s.path.name for s in message.sentences], message.audio,
                          message.duration_s))

    def test_no_file_gives_empty_snapshot_silently(self):
        with self.assertNoLogs("tts_player.saved_messages", "WARNING"):
            loaded = self.saved.load()
        self.assertEqual(QueueSnapshot([], [], {}, 0), loaded)

    def test_corrupt_file_warns_starts_empty_and_is_deleted(self):
        for text in ("{не json", "[]", '{"messages": [{"text": 1}]}'):
            self.path.write_text(text, encoding="utf-8")
            with self.assertLogs("tts_player.saved_messages", "WARNING"):
                loaded = self.saved.load()
            self.assertEqual([], loaded.messages)
            self.assertFalse(self.path.exists())

    def test_save_failure_warns(self):
        saved = SavedMessages(self.store / "missing" / "messages.json", self.store)
        with self.assertLogs("tts_player.saved_messages", "WARNING"):
            saved.save(self.snapshot())


if __name__ == "__main__":
    unittest.main()
