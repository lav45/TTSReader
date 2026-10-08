"""Тесты History: чтение склеек демона из speech_history и базы handy_analysis.db."""
import logging
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from tts_player.history import History, HistoryEntry  # noqa: E402

TIMEOUT_MS = 8000
QUIET_MS = 600

# Схема из claude_code_tts/handy.py (_init_speech_history_db).
SCHEMA = """
    CREATE TABLE IF NOT EXISTS speech_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_name TEXT NOT NULL,
        created_at REAL NOT NULL,
        session_id TEXT,
        project TEXT,
        persona TEXT,
        text TEXT,
        speed REAL,
        tone TEXT,
        duration_seconds REAL
    )
"""
NAME = "tts-1760000000.123-sess-1.wav"


class HistoryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.dir = self.root / "speech_history"
        self.dir.mkdir()
        self.db = self.root / "handy_analysis.db"
        conn = sqlite3.connect(str(self.db))
        conn.execute(SCHEMA)
        conn.commit()
        conn.close()
        self.got: list[HistoryEntry] = []
        self.history = History(self.dir, self.db, self.got.append)
        self.addCleanup(self.history.stop)

    def daemon_write_file(self, name=NAME):
        """Демон копирует WAV (copy2, не атомарный rename)."""
        src = self.root / "src.wav"
        src.write_bytes(b"RIFF....WAVE")
        shutil.copy2(src, self.dir / name)

    def daemon_insert_row(self, name=NAME, created_at=1760000000.123,
                          session_id="sess-1", project="proj", duration=2.5):
        conn = sqlite3.connect(str(self.db))
        conn.execute(
            "INSERT INTO speech_history (file_name, created_at, session_id, project,"
            " persona, text, speed, tone, duration_seconds) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, created_at, session_id, project, "p", "t", 1.0, "neutral", duration),
        )
        conn.commit()
        conn.close()

    def spin(self, until=None, quiet=False):
        loop = GLib.MainLoop()
        GLib.timeout_add(QUIET_MS if quiet else TIMEOUT_MS, loop.quit)
        if until:
            def check():
                if until():
                    loop.quit()
                    return False
                return True
            GLib.timeout_add(20, check)
        loop.run()

    def test_new_file_with_row_delivers_one_entry(self):
        self.history.start()
        self.daemon_insert_row()
        self.daemon_write_file()
        self.spin(until=lambda: self.got)
        self.spin(quiet=True)
        self.assertEqual(self.got, [HistoryEntry(
            path=self.dir / NAME, created_at=1760000000.123,
            session_id="sess-1", project="proj", duration_s=2.5)])

    def test_row_written_after_file_still_delivers(self):
        self.history.start()
        self.daemon_write_file()
        GLib.timeout_add(1000, lambda: self.daemon_insert_row() and False)
        self.spin(until=lambda: self.got)
        self.assertEqual(len(self.got), 1)
        self.assertEqual(self.got[0].path, self.dir / NAME)

    def test_file_without_row_warns_after_5s_and_no_entry(self):
        self.history.start()
        self.daemon_write_file()
        loop = GLib.MainLoop()
        GLib.timeout_add(6000, loop.quit)
        with self.assertLogs("tts_player.history", level=logging.WARNING) as logs:
            loop.run()
        self.assertEqual(self.got, [])
        self.assertEqual(len([m for m in logs.output if NAME in m]), 1)

    def test_files_before_start_are_ignored(self):
        self.daemon_write_file()
        self.daemon_insert_row()
        self.history.start()
        self.spin(quiet=True)
        self.assertEqual(self.got, [])

    def test_foreign_names_are_ignored(self):
        self.history.start()
        for name in (".tts-1760000000.123-s.wav", "other.wav", "tts-1760000000.123-s.wav.part",
                     "tts-1760000000.123-s.mp3"):
            self.daemon_write_file(name)
            self.daemon_insert_row(name)
        self.spin(quiet=True)
        self.assertEqual(self.got, [])

    def test_database_is_not_modified(self):
        self.daemon_insert_row()
        before = self.db.read_bytes()
        mtime = self.db.stat().st_mtime_ns
        self.history.start()
        self.daemon_write_file()
        self.spin(until=lambda: self.got)
        self.history.stop()
        self.assertEqual(len(self.got), 1)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(self.db.stat().st_mtime_ns, mtime)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith("handy")],
                         ["handy_analysis.db"])

    def test_missing_database_retries_and_delivers_when_it_appears(self):
        self.db.rename(self.root / "later.db")
        self.history.start()
        self.daemon_write_file()
        GLib.timeout_add(800, lambda: (self.root / "later.db").rename(self.db) and False)
        GLib.timeout_add(800, lambda: self.daemon_insert_row() and False)
        self.spin(until=lambda: self.got)
        self.assertEqual(len(self.got), 1)

    def test_delivers_each_file_once(self):
        self.history.start()
        self.daemon_insert_row()
        self.daemon_write_file()
        self.spin(until=lambda: self.got)
        (self.dir / NAME).write_bytes(b"changed again")
        self.spin(quiet=True)
        self.assertEqual(len(self.got), 1)


if __name__ == "__main__":
    unittest.main()
