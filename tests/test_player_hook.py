"""Тесты bin/tts-player-hook: демон с обёрткой в PATH, плеер в трее, регистрация сессии.

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
        self.stub("claude-tts", starts_daemon=True)
        self.stub("tts-player")

    def stub(self, name, starts_daemon=False):
        """Заглушка; starts_daemon: «daemon start» оставляет PID-файл и фальшивый /proc."""
        path = self.stubs / name
        body = (
            "#!/bin/bash\n"
            f'echo "$*" > "{self.records}/{name}.args"\n'
            f'echo "$PATH" > "{self.records}/{name}.path"\n'
            f'echo "$*" >> "{self.records}/{name}.calls"\n'
            'echo "вывод заглушки $1"\n')
        if starts_daemon:
            body += (
                f'if [ "$*" = "daemon start" ]; then\n'
                f'  echo {PID} > "{self.home}/.claude-tts/daemon.pid"\n'
                f'  mkdir -p "{self.proc}/{PID}"\n'
                f'  printf "python3\\0-m\\0claude_code_tts\\0" > "{self.proc}/{PID}/cmdline"\n'
                "fi\n")
        path.write_text(body)
        path.chmod(0o755)

    def daemon(self, path_value, alive=True, cmdline=b"/usr/bin/python3\0-m\0claude_code_tts\0"):
        """PID-файл демона и (если жив) фальшивый /proc/<pid>/environ и cmdline."""
        (self.home / ".claude-tts" / "daemon.pid").write_text(PID + "\n")
        if alive:
            env_dir = self.proc / PID
            env_dir.mkdir()
            (env_dir / "cmdline").write_bytes(cmdline)
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

    def calls(self, name, count, wait=3.0):
        """Строки журнала вызовов заглушки; ждёт count строк (отвязанные процессы пишут позже)."""
        path = self.records / f"{name}.calls"
        deadline = time.monotonic() + wait
        while True:
            lines = path.read_text().splitlines() if path.exists() else []
            if len(lines) >= count or time.monotonic() >= deadline:
                return lines
            time.sleep(0.05)

    def test_daemon_absent_starts_with_wrapper_first(self):
        result = self.run_hook("on", "u1", "/p/X")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.record("claude-tts", "args"), "daemon start")
        self.assertTrue(self.record("claude-tts", "path").startswith(DAEMON_BIN + ":"))
        self.assertIn("демон запущен", result.stdout)

    def test_foreign_cmdline_counts_as_no_daemon_and_is_not_restarted(self):
        self.daemon("/usr/bin:/bin", cmdline=b"/usr/bin/sleep\0300\0")
        result = self.run_hook("on", "u1", "/p/X")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.record("claude-tts", "args"), "daemon start")
        self.assertNotIn("перезапуск", result.stdout)

    def test_cmdline_with_claude_tts_is_a_daemon(self):
        self.daemon(f"{DAEMON_BIN}:/usr/bin", cmdline=b"claude-tts\0daemon\0run\0")
        result = self.run_hook("on", "u1", "/p/X")
        time.sleep(0.5)
        self.assertFalse((self.records / "claude-tts.args").exists())
        self.assertIn("демон уже с обёрткой", result.stdout)

    def test_failed_start_reports_it(self):
        self.stub("claude-tts")
        result = self.run_hook("on", "u1", "/p/X")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("демон не запустился", result.stdout)
        self.assertNotIn("демон запущен", result.stdout)

    def test_dead_pid_counts_as_absent(self):
        self.daemon("/x", alive=False)
        self.run_hook("on", "u1", "/p/X")
        self.assertEqual(self.record("claude-tts", "args"), "daemon start")

    def test_foreign_path_restarts_with_wrapper_first(self):
        self.daemon("/usr/bin:/bin")
        result = self.run_hook("on", "u1", "/p/X")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.record("claude-tts", "args"), "daemon restart")
        self.assertTrue(self.record("claude-tts", "path").startswith(DAEMON_BIN + ":"))
        self.assertIn("перезапуск демона с обёрткой запущен", result.stdout)

    def test_wrapper_first_leaves_daemon_alone(self):
        self.daemon(f"{DAEMON_BIN}:/usr/bin")
        result = self.run_hook("on", "u1", "/p/X")
        self.assertEqual(result.returncode, 0, result.stderr)
        time.sleep(0.5)
        self.assertFalse((self.records / "claude-tts.args").exists())
        self.assertIn("демон уже с обёрткой", result.stdout)

    def test_player_started_with_tray(self):
        result = self.run_hook("on", "u1", "/p/X")
        self.assertEqual(sorted(self.calls("tts-player", 2)),
                         ["--tray", "session add u1 /p/X"])
        self.assertIn("запуск плеера отдан", result.stdout)
        self.assertIn("player.log", result.stdout)
        self.assertTrue((self.runtime / "tts-player").is_dir())

    def test_restart_warns_that_first_answer_may_miss_player(self):
        self.daemon("/usr/bin:/bin")
        result = self.run_hook("on", "u1", "/p/X")
        self.assertIn("повторите /tts-on через несколько секунд", result.stdout)

    def test_no_restart_has_no_warning(self):
        self.daemon(f"{DAEMON_BIN}:/usr/bin")
        result = self.run_hook("on", "u1", "/p/X")
        self.assertNotIn("повторите /tts-on", result.stdout)

    def test_hook_does_not_wait_for_player(self):
        (self.stubs / "tts-player").write_text("#!/bin/bash\n[ \"$1\" = --tray ] && sleep 5\nexit 0\n")
        started = time.monotonic()
        result = self.run_hook("on", "u1", "/p/X")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - started, 3)

    def test_player_already_running_registers_session_without_play(self):
        lock_dir = self.runtime / "tts-player"
        lock_dir.mkdir()
        with open(lock_dir / "player.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result = self.run_hook("on", "u1", "/p/X")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("tts-player", 1), ["session add u1 /p/X"])
        time.sleep(0.3)
        self.assertEqual(self.calls("tts-player", 1, wait=0), ["session add u1 /p/X"])
        self.assertIn("плеер уже запущен", result.stdout)
        self.assertNotIn("воспроизведение продолжено", result.stdout)
        self.assertIn("вывод заглушки session", result.stdout)

    def test_session_add_stdout_is_passed_through_when_player_starts(self):
        result = self.run_hook("on", "u1", "/p/X")
        self.assertIn("вывод заглушки session", result.stdout)

    def test_bad_arguments_print_usage_and_start_nothing(self):
        for args in (("off",), ("on",), ("on", "u1"), ("on", "u1", "/p", "x"), ("on", "", "/p"), ("on", "u1", ""), ("play",), ()):
            with self.subTest(args=args):
                result = self.run_hook(*args)
                self.assertEqual(result.returncode, 1)
                self.assertIn("Usage: tts-player-hook on <uuid> <cwd>", result.stderr)
                time.sleep(0.1)
                self.assertFalse((self.records / "tts-player.calls").exists())
                self.assertFalse((self.records / "claude-tts.args").exists())

    def test_missing_claude_tts_exits_1(self):
        (self.stubs / "claude-tts").unlink()
        result = self.run_hook("on", "u1", "/p/X", path=f"{self.stubs}:/usr/bin:/bin")
        self.assertEqual(result.returncode, 1)
        self.assertIn("claude-tts", result.stderr)

    def test_missing_tts_player_exits_1(self):
        (self.stubs / "tts-player").unlink()
        result = self.run_hook("on", "u1", "/p/X")
        self.assertEqual(result.returncode, 1)
        self.assertIn("tts-player", result.stderr)


if __name__ == "__main__":
    unittest.main()
