"""Тесты Sessions: реестр сессий плеера и файл sessions.json (контракт 15)."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from tts_player.sessions import Session, Sessions, read_sessions, sessions_path


class SessionsCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = sessions_path(self.root)
        self.sessions = Sessions(self.path)

    def file_data(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write_raw(self, text):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")


class PathTest(unittest.TestCase):
    def test_sessions_path(self):
        self.assertEqual(sessions_path(Path("/run/user/1")), Path("/run/user/1/tts-player/sessions.json"))


class AddTest(SessionsCase):
    def test_first_add_writes_contract_file(self):
        self.assertTrue(self.sessions.add("u1", "/home/u/Programs/TTSReader/"))
        self.assertEqual(self.file_data(), {"active": "u1", "sessions": [{"id": "u1", "project": "TTSReader"}]})

    def test_second_add_appends_and_keeps_active(self):
        self.sessions.add("u1", "/a/one")
        self.sessions.add("u2", "/a/two")
        self.assertEqual(self.sessions.sessions, [Session("u1", "one"), Session("u2", "two")])
        self.assertEqual(self.sessions.active, "u1")
        self.assertEqual(self.file_data()["active"], "u1")

    def test_repeated_add_returns_false_and_does_not_rewrite(self):
        self.sessions.add("u1", "/a/one")
        os.utime(self.path, (1, 1))
        before = self.path.read_text(encoding="utf-8")
        self.assertFalse(self.sessions.add("u1", "/a/other"))
        self.assertEqual(self.path.stat().st_mtime, 1)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_has(self):
        self.sessions.add("u1", "/a/one")
        self.assertTrue(self.sessions.has("u1"))
        self.assertFalse(self.sessions.has("u2"))

    def test_sessions_property_is_copy(self):
        self.sessions.add("u1", "/a/one")
        self.sessions.sessions.clear()
        self.assertEqual(len(self.sessions.sessions), 1)


class RemoveTest(SessionsCase):
    def test_remove_active_resets_active(self):
        self.sessions.add("u1", "/a/one")
        self.assertTrue(self.sessions.remove("u1"))
        self.assertEqual(self.sessions.sessions, [])
        self.assertIsNone(self.sessions.active)
        self.assertEqual(self.file_data(), {"active": None, "sessions": []})

    def test_remove_inactive_keeps_active(self):
        self.sessions.add("u1", "/a/one")
        self.sessions.add("u2", "/a/two")
        self.assertTrue(self.sessions.remove("u2"))
        self.assertEqual(self.sessions.sessions, [Session("u1", "one")])
        self.assertEqual(self.sessions.active, "u1")
        self.assertEqual(self.file_data()["active"], "u1")

    def test_remove_missing_returns_false(self):
        self.assertFalse(self.sessions.remove("nope"))


class LoadTest(SessionsCase):
    def test_load_after_other_instance_wrote(self):
        self.sessions.add("u1", "/a/one")
        self.sessions.add("u2", "/a/two")
        other = Sessions(self.path)
        other.load()
        self.assertEqual(other.sessions, [Session("u1", "one"), Session("u2", "two")])
        self.assertEqual(other.active, "u1")

    def test_load_without_file(self):
        self.sessions.load()
        self.assertEqual(self.sessions.sessions, [])
        self.assertIsNone(self.sessions.active)

    def test_load_broken_files_warn_and_empty(self):
        broken = [
            "не json",
            json.dumps({"active": None, "sessions": "x"}),
            json.dumps({"active": "zz", "sessions": [{"id": "u1", "project": "p"}]}),
        ]
        for text in broken:
            with self.subTest(text=text):
                self.write_raw(text)
                with self.assertLogs("tts_player.sessions", "WARNING"):
                    self.sessions.load()
                self.assertEqual(self.sessions.sessions, [])
                self.assertIsNone(self.sessions.active)


class WriteFailureTest(SessionsCase):
    def test_readonly_dir_warns_and_keeps_memory(self):
        self.path.parent.mkdir(parents=True)
        self.path.parent.chmod(0o500)
        self.addCleanup(self.path.parent.chmod, 0o700)
        with self.assertLogs("tts_player.sessions", "WARNING"):
            self.assertTrue(self.sessions.add("u1", "/a/one"))
        self.assertEqual(self.sessions.sessions, [Session("u1", "one")])
        self.assertEqual(self.sessions.active, "u1")


class ReadSessionsTest(SessionsCase):
    def test_missing_file(self):
        self.assertEqual(read_sessions(self.path), (None, []))

    def test_valid_file(self):
        self.write_raw(json.dumps({"active": "u1", "sessions": [{"id": "u1", "project": "p"}]}))
        self.assertEqual(read_sessions(self.path), ("u1", [Session("u1", "p")]))

    def test_broken_file_raises_value_error(self):
        self.write_raw("{")
        with self.assertRaises(ValueError):
            read_sessions(self.path)

    def test_wrong_shape_raises_value_error(self):
        for data in ([], {"active": None}, {"active": None, "sessions": [1]},
                     {"active": 5, "sessions": []}):
            with self.subTest(data=data):
                self.write_raw(json.dumps(data))
                with self.assertRaises(ValueError):
                    read_sessions(self.path)


if __name__ == "__main__":
    unittest.main()
