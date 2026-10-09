"""Тесты DaemonQueue: запись задачи в очередь демона (временный каталог, не ~/.claude-tts)."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from tts_player.daemon_queue import DaemonQueue


class DaemonQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.queue = DaemonQueue(self.dir, clock=lambda: 1234.5, new_id=lambda: "abc123")

    def test_writes_named_file_with_replaced_fields(self) -> None:
        fields = {"text": "привет", "id": "old", "timestamp": 1, "source": "x", "persona": "ru-irina"}
        result = self.queue.put(fields)
        self.assertEqual(result, "abc123")
        path = self.dir / "1234.500000_abc123.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["text"], "привет")
        self.assertEqual(data["persona"], "ru-irina")
        self.assertEqual(data["id"], "abc123")
        self.assertEqual(data["timestamp"], 1234.5)
        self.assertEqual(data["source"], "tts-player")
        self.assertEqual(fields["id"], "old")

    def test_no_temp_files_left(self) -> None:
        self.queue.put({"text": "a"})
        names = [p.name for p in self.dir.iterdir()]
        self.assertEqual(names, ["1234.500000_abc123.json"])

    def test_temp_name_invisible_to_json_glob(self) -> None:
        seen = []
        original = os.replace

        def spy(src, dst):
            seen.append(Path(src).name)
            original(src, dst)

        os.replace = spy
        try:
            self.queue.put({"text": "a"})
        finally:
            os.replace = original
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0].endswith(".json"))

    @unittest.skipIf(os.geteuid() == 0, "root игнорирует права")
    def test_readonly_dir_returns_none_and_warns(self) -> None:
        self.dir.chmod(0o500)
        self.addCleanup(self.dir.chmod, 0o700)
        with self.assertLogs("tts_player.daemon_queue", level="WARNING"):
            result = self.queue.put({"text": "a"})
        self.assertIsNone(result)

    def test_missing_dir_returns_none(self) -> None:
        queue = DaemonQueue(self.dir / "нет", clock=lambda: 1.0, new_id=lambda: "i")
        with self.assertLogs("tts_player.daemon_queue", level="WARNING"):
            self.assertIsNone(queue.put({"text": "a"}))


if __name__ == "__main__":
    unittest.main()
