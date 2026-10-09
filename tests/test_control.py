"""Тесты ControlRequests: забор запросов session add/remove из control/."""
import json
import logging
import tempfile
import unittest
from pathlib import Path

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from tts_player.control import ControlRequests  # noqa: E402

TIMEOUT_MS = 5000
QUIET_MS = 500


class ControlCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.control_dir = Path(tmp.name) / "control"
        self.control_dir.mkdir()
        self.calls: list[tuple] = []
        self.control = ControlRequests(
            self.control_dir,
            lambda session, cwd: self.calls.append(("add", session, cwd)),
            lambda session: self.calls.append(("remove", session)))
        self.addCleanup(self.control.stop)

    def write(self, name, data):
        path = self.control_dir / name
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

    def start_logged(self) -> list[str]:
        records: list[str] = []

        class Collect(logging.Handler):
            def emit(self, record):
                records.append(f"{record.levelname} {record.getMessage()}")

        handler = Collect()
        logger = logging.getLogger("tts_player.control")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        self.control.start()
        return records

    def test_file_after_start_calls_on_add_and_is_deleted(self):
        self.control.start()
        path = self.write("1.json", {"op": "add", "session": "u1", "cwd": "/p/TTSReader"})
        self.spin(until=lambda: bool(self.calls))
        self.assertEqual([("add", "u1", "/p/TTSReader")], self.calls)
        self.assertFalse(path.exists())

    def test_files_before_start_taken_in_name_order(self):
        self.write("2.json", {"op": "remove", "session": "u1"})
        self.write("1.json", {"op": "add", "session": "u1", "cwd": "/p"})
        self.control.start()
        self.assertEqual([("add", "u1", "/p"), ("remove", "u1")], self.calls)
        self.assertEqual([], list(self.control_dir.iterdir()))

    def test_bad_requests_deleted_with_warning(self):
        bad = {
            "1.json": "не json",
            "2.json": "[1]",
            "3.json": {"op": "bogus", "session": "u1"},
            "4.json": {"op": "add", "cwd": "/p"},
            "5.json": {"op": "add", "session": "u1"},
            "6.json": {"op": "add", "session": "u1", "cwd": 5},
            "7.json": {"op": "remove", "session": 5},
        }
        for name, data in bad.items():
            self.write(name, data)
        records = self.start_logged()
        self.assertEqual([], self.calls)
        self.assertEqual([], list(self.control_dir.iterdir()))
        self.assertEqual(len(bad), len([r for r in records if r.startswith("WARNING")]))

    def test_tmp_and_dot_names_not_taken(self):
        tmp = self.write("1.json.abc.tmp", {"op": "add", "session": "u1", "cwd": "/p"})
        hidden = self.write(".2.json", {"op": "add", "session": "u2", "cwd": "/p"})
        self.control.start()
        self.assertEqual([], self.calls)
        self.assertFalse(tmp.exists())  # остаток писателя убирает старт
        self.assertTrue(hidden.exists())

    def test_start_removes_tmp_leftovers_but_not_json(self):
        tmp = self.write("1.json.abc.tmp", "{")
        self.write("2.json", {"op": "add", "session": "u1", "cwd": "/p"})
        self.control.start()
        self.assertFalse(tmp.exists())
        self.assertEqual([("add", "u1", "/p")], self.calls)

    def test_tmp_after_start_left_alone(self):
        self.control.start()
        tmp = self.write("1.json.abc.tmp", "{")
        self.spin()
        self.assertEqual([], self.calls)
        self.assertTrue(tmp.exists())

    def test_rename_into_json_is_taken(self):
        self.control.start()
        tmp = self.write("1.json.abc.tmp", {"op": "remove", "session": "u1"})
        tmp.rename(self.control_dir / "1.json")
        self.spin(until=lambda: bool(self.calls))
        self.assertEqual([("remove", "u1")], self.calls)

    def test_drain_takes_synchronously_without_loop(self):
        self.write("1.json", {"op": "add", "session": "u1", "cwd": "/p"})
        self.control.drain()
        self.assertEqual([("add", "u1", "/p")], self.calls)
        self.assertEqual([], list(self.control_dir.iterdir()))

    def test_failing_callback_does_not_leave_file(self):
        def boom(session, cwd):
            raise RuntimeError("x")

        control = ControlRequests(self.control_dir, boom, lambda s: None)
        path = self.write("1.json", {"op": "add", "session": "u1", "cwd": "/p"})
        control.drain()
        self.assertFalse(path.exists())

    def test_stop_before_start_is_harmless(self):
        self.control.stop()


if __name__ == "__main__":
    unittest.main()
