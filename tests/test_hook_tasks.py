"""Тесты HookTasks: забор задач хука из queue/ — скан, монитор, битые файлы."""
import json
import logging
import tempfile
import unittest
from pathlib import Path

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from tts_player.hook_tasks import HookTasks  # noqa: E402
from tts_player.model import HookTask  # noqa: E402

TIMEOUT_MS = 5000
QUIET_MS = 500


def task_fields(task_id="a1", text="Привет.") -> dict:
    return {"id": task_id, "timestamp": 12.5, "session_id": "sess", "project": "proj",
            "text": text, "persona": "p", "speed": 1.25, "speed_method": "x", "voice_a": "b"}


class HookTasksCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.queue_dir = Path(tmp.name) / "queue"
        self.queue_dir.mkdir()
        self.got: list[HookTask] = []
        self.tasks = HookTasks(self.queue_dir, self.got.append)
        self.addCleanup(self.tasks.stop)

    def write(self, name, data):
        path = self.queue_dir / name
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return path

    def spin(self, until=None):
        loop = GLib.MainLoop()
        GLib.timeout_add(QUIET_MS if until is None else TIMEOUT_MS, loop.quit)
        if until:
            def check():
                if until():
                    loop.quit()
                    return False
                return True
            GLib.timeout_add(20, check)
        loop.run()

    def start_quiet(self):
        """start() с перехватом журнала (в процессе он идёт в stderr): возвращает записанное."""
        records: list[str] = []

        class Collect(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Collect()
        logger = logging.getLogger("tts_player.hook_tasks")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        self.tasks.start()
        return "\n".join(records)

    def test_scan_takes_in_name_order_and_deletes(self):
        self.write("2.json", task_fields("b"))
        path = self.write("1.json", task_fields("a"))
        self.start_quiet()
        self.assertEqual(["a", "b"], [t.id for t in self.got])
        self.assertFalse(path.exists())
        self.assertEqual([], list(self.queue_dir.iterdir()))

    def test_fields_are_taken_from_file(self):
        self.write("1.json", task_fields())
        self.start_quiet()
        task = self.got[0]
        self.assertEqual(("a1", 12.5, "sess", "proj", "Привет.", "p", 1.25),
                         (task.id, task.timestamp, task.session_id, task.project, task.text,
                          task.persona, task.speed))
        self.assertEqual(task_fields(), task.fields)

    def test_new_file_is_taken_by_monitor(self):
        self.start_quiet()
        path = self.write("5.json", task_fields("n"))
        self.spin(lambda: self.got)
        self.assertEqual(["n"], [t.id for t in self.got])
        self.assertFalse(path.exists())

    def test_tmp_is_ignored_and_rename_is_taken(self):
        self.start_quiet()
        tmp = self.write("6.tmp", task_fields("t"))
        self.spin()
        self.assertEqual([], self.got)
        self.assertTrue(tmp.exists())
        tmp.rename(self.queue_dir / "6.json")
        self.spin(lambda: self.got)
        self.assertEqual(["t"], [t.id for t in self.got])

    def test_hidden_json_is_ignored(self):
        hidden = self.write(".7.json", task_fields("h"))
        self.start_quiet()
        self.assertEqual([], self.got)
        self.assertTrue(hidden.exists())

    def test_broken_files_are_deleted_with_stderr(self):
        broken = {
            "1.json": "{not json",
            "2.json": "[1, 2]",
            "3.json": {"id": "x"},
            "4.json": {"text": "t"},
            "5.json": {"id": 5, "text": "t"},
            "6.json": {"id": "x", "text": 7},
        }
        for name, data in broken.items():
            self.write(name, data)
        stderr = self.start_quiet()
        self.assertEqual([], self.got)
        self.assertEqual([], list(self.queue_dir.iterdir()))
        for name in broken:
            self.assertIn(name, stderr)

    def test_bad_timestamp_or_speed_gets_default_with_warning(self):
        for index, bad in enumerate(("abc", None, [1], {"a": 1})):
            fields = {"id": f"x{index}", "text": "t", "timestamp": bad, "speed": bad}
            self.write(f"{index}.json", fields)
        stderr = self.start_quiet()
        self.assertEqual(4, len(self.got))
        for task in self.got:
            self.assertEqual((0.0, 1.0), (task.timestamp, task.speed))
        self.assertEqual([], list(self.queue_dir.iterdir()))
        self.assertIn("timestamp", stderr)
        self.assertIn("speed", stderr)

    def test_start_mkdir_error_is_logged_not_raised(self):
        blocker = self.queue_dir / "file"
        blocker.write_text("x")
        tasks = HookTasks(blocker / "queue", self.got.append)
        self.addCleanup(tasks.stop)
        records: list[str] = []

        class Collect(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Collect()
        logger = logging.getLogger("tts_player.hook_tasks")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        tasks.start()
        self.assertTrue(records)

    def test_optional_fields_default(self):
        self.write("1.json", {"id": "x", "text": "t"})
        self.start_quiet()
        task = self.got[0]
        self.assertEqual(("", 0.0, "", "", "", 1.0),
                         (task.session_id, task.timestamp, task.project, task.persona, "", task.speed))

    def test_on_task_failure_still_deletes_and_continues(self):
        def on_task(task):
            if task.id == "a":
                raise RuntimeError("boom")
            self.got.append(task)
        self.tasks = HookTasks(self.queue_dir, on_task)
        self.addCleanup(self.tasks.stop)
        self.write("1.json", task_fields("a"))
        self.write("2.json", task_fields("b"))
        self.start_quiet()
        self.assertEqual(["b"], [t.id for t in self.got])
        self.assertEqual([], list(self.queue_dir.iterdir()))


if __name__ == "__main__":
    unittest.main()
