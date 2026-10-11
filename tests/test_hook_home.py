"""Тесты HookHome: runtime_dir и tts_root — временные каталоги, часы подставные."""
import tests  # noqa: F401 — окружение тестов (tests/__init__.py)
import os
import stat
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from tts_player.hook_home import HookHome

NOW = 1000.0


class HookHomeCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runtime = Path(tmp.name) / "run"
        self.root = Path(tmp.name) / "tts"
        self.runtime.mkdir()
        self.root.mkdir()
        self.now = NOW
        self.hook = HookHome(self.runtime, self.root, clock=lambda: self.now)
        self.beat_file = self.hook.home / ".claude-tts" / "daemon.heartbeat"

    def test_paths(self):
        self.assertEqual(self.hook.home, self.runtime / "hook-home")
        self.assertEqual(self.hook.queue_dir, self.runtime / "hook-home" / ".claude-tts" / "queue")

    def test_prepare_links_and_queue(self):
        self.hook.prepare()
        base = self.hook.home / ".claude-tts"
        self.assertTrue(self.hook.queue_dir.is_dir())
        self.assertEqual(os.readlink(base / "config.json"), str(self.root / "config.json"))
        self.assertEqual(os.readlink(base / "sessions.d"), str(self.root / "sessions.d"))

    def test_prepare_twice_cleans_queue_and_replaces_links(self):
        self.hook.prepare()
        (self.hook.queue_dir / "a.json").write_text("{}")
        (self.hook.queue_dir / "b.tmp").write_text("{}")
        (self.hook.home / ".claude-tts" / "config.json").unlink()
        (self.hook.home / ".claude-tts" / "config.json").write_text("plain")
        self.hook.prepare()
        self.assertEqual(list(self.hook.queue_dir.iterdir()), [])
        self.assertTrue((self.hook.home / ".claude-tts" / "config.json").is_symlink())

    def test_prepare_real_directory_at_link_name_does_not_stop_the_rest(self):
        base = self.hook.home / ".claude-tts"
        (base / "config.json").mkdir(parents=True)
        self.hook.queue_dir.mkdir(parents=True)
        (self.hook.queue_dir / "a.json").write_text("{}")
        with self.assertLogs("tts_player.hook_home", level="WARNING"):
            self.hook.prepare()
        self.assertTrue((base / "config.json").is_dir())
        self.assertFalse((base / "config.json").is_symlink())
        self.assertEqual(os.readlink(base / "sessions.d"), str(self.root / "sessions.d"))
        self.assertEqual([], list(self.hook.queue_dir.iterdir()))

    def test_prepare_creates_missing_sessions_dir(self):
        self.hook.prepare()
        self.assertTrue((self.root / "sessions.d").is_dir())

    def test_prepare_removes_leftover_heartbeat_tmp(self):
        base = self.hook.home / ".claude-tts"
        base.mkdir(parents=True)
        (base / "daemon.heartbeat.abc.tmp").write_text("1")
        self.hook.prepare()
        self.assertEqual([], list(base.glob("daemon.heartbeat.*.tmp")))

    def test_pid_permission_error_is_dead(self):
        self.hook.prepare()
        (self.root / "daemon.pid").write_text("1")
        with unittest.mock.patch("os.kill", side_effect=PermissionError):
            self.assertFalse(self.hook.beat())

    def test_beat_fresh_real_heartbeat(self):
        self.hook.prepare()
        (self.root / "daemon.heartbeat").write_text(str(NOW - 10))
        self.assertTrue(self.hook.beat())
        self.assertEqual(float(self.beat_file.read_text()), NOW)

    def test_beat_stale_and_no_pid_removes(self):
        self.hook.prepare()
        self.beat_file.write_text("1")
        (self.root / "daemon.heartbeat").write_text(str(NOW - 100))
        self.assertFalse(self.hook.beat())
        self.assertFalse(self.beat_file.exists())

    def test_beat_alive_pid_without_heartbeat(self):
        self.hook.prepare()
        (self.root / "daemon.pid").write_text(str(os.getpid()))
        self.assertTrue(self.hook.beat())
        self.assertTrue(self.beat_file.exists())

    def test_beat_dead_pid(self):
        self.hook.prepare()
        (self.root / "daemon.pid").write_text("999999999")
        self.assertFalse(self.hook.beat())

    def test_stop_removes_heartbeat_and_timer(self):
        (self.root / "daemon.heartbeat").write_text(str(NOW))
        self.hook.start()
        self.assertTrue(self.beat_file.exists())
        self.assertIsNotNone(self.hook._timer_id)
        self.hook.stop()
        self.assertFalse(self.beat_file.exists())
        self.assertIsNone(self.hook._timer_id)

    @unittest.skipIf(os.geteuid() == 0, "root игнорирует права")
    def test_write_error_logged_not_raised(self):
        self.hook.prepare()
        (self.root / "daemon.heartbeat").write_text(str(NOW))
        base = self.hook.home / ".claude-tts"
        base.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(base.chmod, 0o755)
        with self.assertLogs("tts_player.hook_home", level="WARNING"):
            self.assertFalse(self.hook.beat())


if __name__ == "__main__":
    unittest.main()
