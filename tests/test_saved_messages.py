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
        unheard = Message("S", project="/w/p", text="Привет", persona="p", speed=1.5,
                          task_ids={"aa", "bb"},
                          sentences=[self.sentence(S0, 0), self.sentence(S1, 1)],
                          received_at=5.0, sentence_count=2)
        heard = Message("N", text="пока", persona="q", speed=1.0, task_ids={"cc"},
                        received_at=6.0, heard=True, sentence_count=3,
                        played_indices={0, 2})
        not_sent = Message("S", text="x", sent=False)
        deferred = Message("B", text="отложено", persona="p", speed=1.0, received_at=8.0,
                           sent=False, deferred=True)
        return QueueSnapshot([unheard, heard, not_sent, deferred], [unheard],
                             {"aa": 1, "bb": 3, "cc": 2}, 3)

    def fields(self, message: Message) -> tuple:
        return (message.session_id, message.project, message.text, message.persona,
                message.speed, message.task_ids, message.sentences, message.received_at,
                message.sent, message.heard, message.deferred, message.sentence_count,
                message.played_indices)

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

    def test_missing_sentence_count_reads_as_unknown(self):
        """Файл до счётчика предложений: число неизвестно (его запросит Player.start),
        доигранных индексов нет."""
        self.saved.save(self.snapshot())
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual([2, 3, None, None], [m["sentence_count"] for m in data["messages"]])
        self.assertEqual([0, 2], data["messages"][1]["played_indices"])
        for item in data["messages"]:
            del item["sentence_count"]
            del item["played_indices"]
        self.path.write_text(json.dumps(data), encoding="utf-8")
        loaded = self.saved.load().messages
        self.assertEqual([(None, set())] * 4,
                         [(m.sentence_count, m.played_indices) for m in loaded])

    def test_saved_file_names_are_relative_to_store(self):
        self.saved.save(self.snapshot())
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual([S0, S1], data["messages"][0]["sentences"])

    def test_joined_file_keys_are_not_written(self):
        self.saved.save(self.snapshot())
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual([], [key for m in data["messages"] for key in m
                              if key in ("audio", "created_at", "duration_s")])

    def test_joined_file_keys_of_older_file_are_ignored(self):
        """Файл до отказа от склейки демона: ключи audio/created_at/duration_s не мешают
        чтению, склейка из store/ сообщению не достаётся (её удалит старт как ничью)."""
        snapshot = self.snapshot()
        self.saved.save(snapshot)
        (self.store / "tts-1.wav").write_bytes(b"wav")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        for item in data["messages"]:
            item.update({"audio": "tts-1.wav", "created_at": 7.0, "duration_s": 1.0})
        self.path.write_text(json.dumps(data), encoding="utf-8")
        loaded = self.saved.load()
        self.assertEqual([self.fields(m) for m in snapshot.messages],
                         [self.fields(m) for m in loaded.messages])

    def test_missing_files_are_dropped(self):
        """Пропавшее предложение выпадает; без всех — «без звука» и при play
        переозвучивается."""
        self.saved.save(self.snapshot())
        (self.store / S1).unlink()
        message = self.saved.load().messages[0]
        self.assertEqual([S0], [s.path.name for s in message.sentences])

    def test_no_file_gives_empty_snapshot_silently(self):
        with self.assertNoLogs("tts_player.saved_messages", "WARNING"):
            loaded = self.saved.load()
        self.assertEqual(QueueSnapshot([], [], {}, 0), loaded)

    def test_corrupt_file_warns_starts_empty_and_is_deleted(self):
        bad_count = json.dumps({"version": 1, "task_order": {}, "delivered": 0, "messages": [
            {"session_id": "S", "project": None, "text": None, "persona": None, "speed": None,
             "task_ids": [], "received_at": None, "sent": True, "heard": False,
             "started": False, "sentences": [], "sentence_count": -1}]})
        for text in ("{не json", "[]", '{"messages": [{"text": 1}]}', bad_count):
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
