"""Тесты bin/tts-player-hook start|end: демон с обёрткой в PATH, плеер в трее, регистрация
и снятие сессии по JSON хука на stdin; stdout пуст, строки состояния - в hook.log.

Заглушки claude-tts и tts-player стоят первыми в PATH и пишут свои аргументы и PATH
в файлы; /proc и HOME подменены переменными TTS_HOOK_PROC и TTS_HOOK_HOME.
"""
import fcntl
import json
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
        self.addCleanup(self.wait_stubs_finished)  # после tmp.cleanup в обратном порядке: до удаления каталога

    def wait_stubs_finished(self, wait=15.0):
        """Отвязанные (setsid) заглушки пишут в records/ после возврата хука: каталог удаляется,
        только когда ни один процесс заглушек не жив."""
        deadline = time.monotonic() + wait
        while subprocess.run(["pgrep", "-f", str(self.stubs)], capture_output=True).returncode == 0:
            self.assertLess(time.monotonic(), deadline, "заглушки не завершились")
            time.sleep(0.05)

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

    def run_hook(self, *args, path=None, stdin=None):
        """Запуск хука; stdin - строка или None (тогда пусто); stdout хука обязан быть пуст."""
        env = {
            "PATH": path if path is not None else f"{self.stubs}:/usr/bin:/bin",
            "XDG_RUNTIME_DIR": str(self.runtime),
            "TTS_HOOK_HOME": str(self.home),
            "TTS_HOOK_PROC": str(self.proc),
        }
        result = subprocess.run([str(HOOK), *args], env=env, capture_output=True,
                                text=True, timeout=10, input=stdin if stdin is not None else "")
        self.assertEqual(result.stdout, "", "stdout хука должен быть пуст")
        return result

    def start(self, session="u1", cwd="/p/X", **kwargs):
        payload = json.dumps({"session_id": session, "cwd": cwd, "source": "startup"})
        return self.run_hook("start", stdin=payload, **kwargs)

    def end(self, session="u1", **kwargs):
        payload = json.dumps({"session_id": session, "reason": "clear"})
        return self.run_hook("end", stdin=payload, **kwargs)

    def hook_log(self):
        path = self.runtime / "tts-player" / "hook.log"
        return path.read_text() if path.exists() else ""

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
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.record("claude-tts", "args"), "daemon start")
        self.assertTrue(self.record("claude-tts", "path").startswith(DAEMON_BIN + ":"))
        self.assertIn("демон запущен", self.hook_log())

    def test_foreign_cmdline_counts_as_no_daemon_and_is_not_restarted(self):
        self.daemon("/usr/bin:/bin", cmdline=b"/usr/bin/sleep\0300\0")
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.record("claude-tts", "args"), "daemon start")
        self.assertNotIn("перезапуск", self.hook_log())

    def test_cmdline_with_claude_tts_is_a_daemon(self):
        self.daemon(f"{DAEMON_BIN}:/usr/bin", cmdline=b"claude-tts\0daemon\0run\0")
        result = self.start()
        time.sleep(0.5)
        self.assertFalse((self.records / "claude-tts.args").exists())
        self.assertIn("демон уже с обёрткой", self.hook_log())

    def test_failed_start_reports_it(self):
        self.stub("claude-tts")
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("демон не запустился", self.hook_log())
        self.assertNotIn("демон запущен", self.hook_log())

    def test_dead_pid_counts_as_absent(self):
        self.daemon("/x", alive=False)
        self.start()
        self.assertEqual(self.record("claude-tts", "args"), "daemon start")

    def test_foreign_path_restarts_with_wrapper_first(self):
        self.daemon("/usr/bin:/bin")
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.record("claude-tts", "args"), "daemon restart")
        self.assertTrue(self.record("claude-tts", "path").startswith(DAEMON_BIN + ":"))
        self.assertIn("перезапуск демона с обёрткой запущен", self.hook_log())

    def test_wrapper_first_leaves_daemon_alone(self):
        self.daemon(f"{DAEMON_BIN}:/usr/bin")
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stderr)
        time.sleep(0.5)
        self.assertFalse((self.records / "claude-tts.args").exists())
        self.assertIn("демон уже с обёрткой", self.hook_log())

    def test_player_started_with_tray(self):
        result = self.start()
        self.assertEqual(sorted(self.calls("tts-player", 2)),
                         ["--tray", "session add u1 /p/X"])
        self.assertIn("запуск плеера отдан", self.hook_log())
        self.assertIn("player.log", self.hook_log())
        self.assertTrue((self.runtime / "tts-player").is_dir())

    def test_restart_warns_that_first_answer_may_miss_player(self):
        self.daemon("/usr/bin:/bin")
        result = self.start()
        self.assertIn("может прозвучать мимо плеера", self.hook_log())

    def test_no_restart_has_no_warning(self):
        self.daemon(f"{DAEMON_BIN}:/usr/bin")
        result = self.start()
        self.assertNotIn("мимо плеера", self.hook_log())

    def test_hook_does_not_wait_for_player(self):
        (self.stubs / "tts-player").write_text("#!/bin/bash\n[ \"$1\" = --tray ] && sleep 5\nexit 0\n")
        started = time.monotonic()
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - started, 3)

    def test_player_already_running_registers_session_without_play(self):
        lock_dir = self.runtime / "tts-player"
        lock_dir.mkdir()
        with open(lock_dir / "player.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result = self.start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("tts-player", 1), ["session add u1 /p/X"])
        time.sleep(0.3)
        self.assertEqual(self.calls("tts-player", 1, wait=0), ["session add u1 /p/X"])
        self.assertIn("плеер уже запущен", self.hook_log())
        self.assertNotIn("воспроизведение продолжено", self.hook_log())
        self.assertIn("вывод заглушки session", self.hook_log())

    def test_session_add_output_goes_to_log_when_player_starts(self):
        result = self.start()
        self.assertIn("вывод заглушки session", self.hook_log())

    def test_restart_warning_has_no_tts_on_phrase(self):
        self.daemon("/usr/bin:/bin")
        self.start()
        self.assertNotIn("/tts-on", self.hook_log())

    def test_start_registers_session_with_cwd(self):
        result = self.start(session="u1", cwd="/p/proj")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("session add u1 /p/proj", self.calls("tts-player", 2))

    def test_end_removes_session_and_starts_nothing(self):
        result = self.end()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("tts-player", 1), ["session remove u1"])
        time.sleep(0.3)
        self.assertEqual(self.calls("tts-player", 1, wait=0), ["session remove u1"])
        self.assertFalse((self.records / "claude-tts.args").exists())
        self.assertIn("вывод заглушки session", self.hook_log())

    def test_end_works_without_claude_tts(self):
        (self.stubs / "claude-tts").unlink()
        result = self.end()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("tts-player", 1), ["session remove u1"])

    def test_bad_input_exits_1_and_runs_no_stubs(self):
        inputs = ("не json", "[1]", "{}", '{"session_id": ""}', '{"session_id": 5}', "")
        for mode in ("start", "end"):
            for stdin in inputs + (('{"session_id":"u1"}', '{"session_id":"u1","cwd":""}') if mode == "start" else ()):
                with self.subTest(mode=mode, stdin=stdin):
                    result = self.run_hook(mode, stdin=stdin)
                    self.assertEqual(result.returncode, 1)
                    self.assertNotEqual(result.stderr.strip(), "")
                    time.sleep(0.05)
                    self.assertFalse((self.records / "tts-player.calls").exists())
                    self.assertFalse((self.records / "claude-tts.args").exists())

    def test_bad_arguments_print_usage_and_start_nothing(self):
        payload = json.dumps({"session_id": "u1", "cwd": "/p"})
        for args in (("on", "u1", "/p"), ("off",), ("play",), ("start", "extra"), ("end", "u1"), ()):
            with self.subTest(args=args):
                result = self.run_hook(*args, stdin=payload)
                self.assertEqual(result.returncode, 1)
                self.assertIn("Usage: tts-player-hook start|end", result.stderr)
                time.sleep(0.1)
                self.assertFalse((self.records / "tts-player.calls").exists())
                self.assertFalse((self.records / "claude-tts.args").exists())

    def test_session_add_failure_fails_hook(self):
        (self.stubs / "tts-player").write_text("#!/bin/bash\n[ \"$1\" = --tray ] && exit 0\necho boom\nexit 1\n")
        result = self.start()
        self.assertEqual(result.returncode, 1)
        self.assertNotEqual(result.stderr.strip(), "")
        self.assertIn("boom", self.hook_log())

    def test_session_remove_failure_fails_hook(self):
        (self.stubs / "tts-player").write_text("#!/bin/bash\necho boom\nexit 1\n")
        result = self.end()
        self.assertEqual(result.returncode, 1)
        self.assertNotEqual(result.stderr.strip(), "")

    def test_missing_claude_tts_exits_1(self):
        (self.stubs / "claude-tts").unlink()
        result = self.start()
        self.assertEqual(result.returncode, 1)
        self.assertIn("claude-tts", result.stderr)

    def test_missing_tts_player_exits_1(self):
        (self.stubs / "tts-player").unlink()
        for run in (self.start, self.end):
            with self.subTest(run=run.__name__):
                result = run()
                self.assertEqual(result.returncode, 1)
                self.assertIn("tts-player", result.stderr)


if __name__ == "__main__":
    unittest.main()
