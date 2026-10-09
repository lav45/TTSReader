"""Тесты bin/tts-player-hook: демон с обёрткой в PATH, плеер в трее, off останавливает звук.

Заглушки claude-tts и tts-player стоят первыми в PATH и пишут свои аргументы и PATH
в файлы; /proc и HOME подменены переменными TTS_HOOK_PROC и TTS_HOOK_HOME.
"""
import fcntl
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "bin" / "tts-player-hook"
DAEMON_BIN = str(ROOT / "daemon-bin")
PID = "4242"


class PlayerHookTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.home = self.root / "home"
        (self.home / ".claude-tts").mkdir(parents=True)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.stubs = self.root / "stubs"
        self.stubs.mkdir()
        self.records = self.root / "records"
        self.records.mkdir()
        self.stub("claude-tts")
        self.stub("tts-player")

    def stub(self, name):
        path = self.stubs / name
        path.write_text(
            "#!/bin/bash\n"
            f'echo "$*" > "{self.records}/{name}.args"\n'
            f'echo "$PATH" > "{self.records}/{name}.path"\n')
        path.chmod(0o755)

    def daemon(self, path_value, alive=True):
        """PID-файл демона и (если жив) фальшивый /proc/<pid>/environ."""
        (self.home / ".claude-tts" / "daemon.pid").write_text(PID + "\n")
        if alive:
            env_dir = self.proc / PID
            env_dir.mkdir()
            (env_dir / "environ").write_bytes(
                f"HOME=/x\0PATH={path_value}\0LANG=C\0".encode())

    def run_hook(self, *args, path=None):
        env = {
            "PATH": path if path is not None else f"{self.stubs}:/usr/bin:/bin",
            "XDG_RUNTIME_DIR": str(self.runtime),
            "TTS_HOOK_HOME": str(self.home),
            "TTS_HOOK_PROC": str(self.proc),
        }
        return subprocess.run([str(HOOK), *args], env=env, capture_output=True,
                              text=True, timeout=10)

    def record(self, name, kind, wait=3.0):
        """Содержимое записи заглушки; ждёт файл (отвязанные процессы пишут позже)."""
        path = self.records / f"{name}.{kind}"
        deadline = time.monotonic() + wait
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        return path.read_text().strip() if path.exists() else None

    def test_daemon_absent_starts_with_wrapper_first(self):
        result = self.run_hook("on")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.record("claude-tts", "args"), "daemon start")
        self.assertTrue(self.record("claude-tts", "path").startswith(DAEMON_BIN + ":"))
        self.assertIn("демон запущен", result.stdout)

    def test_dead_pid_counts_as_absent(self):
        self.daemon("/x", alive=False)
        self.run_hook("on")
        self.assertEqual(self.record("claude-tts", "args"), "daemon start")

    def test_foreign_path_restarts_with_wrapper_first(self):
        self.daemon("/usr/bin:/bin")
        result = self.run_hook("on")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.record("claude-tts", "args"), "daemon restart")
        self.assertTrue(self.record("claude-tts", "path").startswith(DAEMON_BIN + ":"))
        self.assertIn("перезапуск демона с обёрткой запущен", result.stdout)

    def test_wrapper_first_leaves_daemon_alone(self):
        self.daemon(f"{DAEMON_BIN}:/usr/bin")
        result = self.run_hook("on")
        self.assertEqual(result.returncode, 0, result.stderr)
        time.sleep(0.5)
        self.assertFalse((self.records / "claude-tts.args").exists())
        self.assertIn("демон уже с обёрткой", result.stdout)

    def test_player_started_with_tray(self):
        result = self.run_hook("on")
        self.assertEqual(self.record("tts-player", "args"), "--tray")
        self.assertIn("плеер запущен", result.stdout)
        self.assertTrue((self.runtime / "tts-player").is_dir())

    def test_hook_does_not_wait_for_player(self):
        (self.stubs / "tts-player").write_text("#!/bin/bash\nsleep 5\n")
        started = time.monotonic()
        result = self.run_hook("on")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - started, 3)

    def test_player_already_running_is_not_launched(self):
        lock_dir = self.runtime / "tts-player"
        lock_dir.mkdir()
        with open(lock_dir / "player.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result = self.run_hook("on")
        time.sleep(0.5)
        self.assertIn("плеер уже запущен", result.stdout)
        self.assertFalse((self.records / "tts-player.args").exists())

    def test_off_calls_stop(self):
        lock_dir = self.runtime / "tts-player"
        lock_dir.mkdir()
        with open(lock_dir / "player.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result = self.run_hook("off")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.record("tts-player", "args"), "stop")
        self.assertIn("звук плеера остановлен", result.stdout)

    def test_off_without_player(self):
        result = self.run_hook("off")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("плеер не запущен", result.stdout)

    def test_missing_claude_tts_exits_1(self):
        (self.stubs / "claude-tts").unlink()
        result = self.run_hook("on", path=f"{self.stubs}:/usr/bin:/bin")
        self.assertEqual(result.returncode, 1)
        self.assertIn("claude-tts", result.stderr)

    def test_missing_tts_player_exits_1(self):
        (self.stubs / "tts-player").unlink()
        result = self.run_hook("off")
        self.assertEqual(result.returncode, 1)
        self.assertIn("tts-player", result.stderr)


if __name__ == "__main__":
    unittest.main()
