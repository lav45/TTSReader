"""Тесты хуков Claude Code (tts_player.hook_command, bin/tts-player-hook).

claude-tts — заглушка первой в PATH: пишет аргументы, окружение и stdin в records/; на
`daemon start` поднимает процесс с claude-tts в команде и кладёт его PID в daemon.pid. Плеер —
заглушка вместо bin/tts-player. HOME (config.json, daemon.pid) и каталог состояния — временные.
"""
import tests  # noqa: F401 — окружение тестов (tests/__init__.py)
import io
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tts_player import hook_command
from tts_player.instance import Instance
from tts_player.sessions import read_sessions, sessions_path

HOOK = Path(__file__).resolve().parent.parent / "bin" / "tts-player-hook"


class HookCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        (self.home / ".claude-tts").mkdir(parents=True)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.records = self.root / "records"
        self.records.mkdir()
        self.stubs = self.root / "stubs"
        self.stubs.mkdir()
        self.sleepers: list[int] = []
        self.addCleanup(self.kill_sleepers)
        self.stub_claude_tts()
        self.player = self.stubs / "tts-player"
        self.player.write_text(f'#!/bin/bash\necho "$*" >> "{self.records}/player.args"\n')
        self.player.chmod(0o755)
        self.config_mode("queue")
        env = {"PATH": f"{self.stubs}:{os.environ.get('PATH', '')}",
               "XDG_RUNTIME_DIR": str(self.runtime), "HOME": str(self.home)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def kill_sleepers(self) -> None:
        pid_file = self.home / ".claude-tts" / "daemon.pid"
        if pid_file.exists():
            self.sleepers.append(int(pid_file.read_text()))
        for pid in self.sleepers:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def stub_claude_tts(self, starts_daemon: bool = True, body: str = "") -> None:
        start = (f'if [ "$*" = "daemon start" ]; then\n'
                 f'  (exec -a claude-tts-daemon sleep 30) &\n'
                 f'  echo $! > "{self.home}/.claude-tts/daemon.pid"\n'
                 f'fi\n') if starts_daemon else ""
        path = self.stubs / "claude-tts"
        path.write_text(
            "#!/bin/bash\n"
            f'echo "$*" >> "{self.records}/claude-tts.args"\n'
            f'echo "session=$CLAUDE_TTS_SESSION queue=$CLAUDE_TTS_QUEUE_DIR" > "{self.records}/claude-tts.env"\n'
            f'[ "$1" = daemon ] || cat > "{self.records}/claude-tts.stdin"\n'
            + start + body)
        path.chmod(0o755)

    def config_mode(self, mode: str | None) -> None:
        data = {} if mode is None else {"mode": mode}
        (self.home / ".claude-tts" / "config.json").write_text(json.dumps(data))

    def running_daemon(self, argv: list[str]) -> None:
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)  # раньше kill_sleepers в очереди — значит, после него
        self.addCleanup(proc.kill)
        (self.home / ".claude-tts" / "daemon.pid").write_text(f"{proc.pid}\n")

    def hook(self, event: str, payload: dict | str) -> tuple[int, str, str]:
        stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = hook_command.main([event], stdin, self.home, self.player)
        return code, out.getvalue(), err.getvalue()

    def start(self, session: str = "u1", cwd: str = "/p/X") -> tuple[int, str, str]:
        result = self.hook("start", {"session_id": session, "cwd": cwd, "source": "startup"})
        self.assertEqual("", result[1], "stdout start должен быть пуст")
        return result

    def end(self, session: str = "u1", reason: str = "other", cwd: str | None = None):
        data = {"session_id": session, "reason": reason, "cwd": cwd}
        result = self.hook("end", {k: v for k, v in data.items() if v is not None})
        self.assertEqual("", result[1], "stdout end должен быть пуст")
        return result

    def hook_log(self) -> str:
        path = self.runtime / "tts-player" / "hook.log"
        return path.read_text() if path.exists() else ""

    def record(self, name: str, wait: float = 3.0) -> str | None:
        """Запись заглушки; ждёт файл (отвязанный процесс пишет позже)."""
        path = self.records / name
        deadline = time.monotonic() + wait
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        return path.read_text().strip() if path.exists() else None

    def control_requests(self) -> list[dict]:
        control = self.runtime / "tts-player" / "control"
        return [json.loads(p.read_text()) for p in sorted(control.glob("*.json"))] if control.is_dir() else []

    def hold_player(self) -> None:
        """Плеер запущен: блокировка экземпляра взята этим процессом."""
        instance = Instance.acquire(self.runtime)
        self.assertIsNotNone(instance)
        self.addCleanup(instance.release)

    def list_session(self, session: str) -> None:
        path = sessions_path(self.runtime)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"active": session, "sessions": [{"id": session, "project": "X"}]}))
        self.assertEqual(session, read_sessions(path)[0])


class StartTest(HookCase):
    def test_daemon_absent_is_started(self) -> None:
        code, _out, err = self.start()
        self.assertEqual(0, code, err)
        self.assertEqual("daemon start", self.record("claude-tts.args"))
        self.assertIn("демон запущен", self.hook_log())

    def test_running_daemon_is_left_alone(self) -> None:
        self.running_daemon(["bash", "-c", "exec -a claude-tts sleep 30"])  # команда — «claude-tts 30»
        self.assertEqual(0, self.start()[0])
        self.assertIsNone(self.record("claude-tts.args", wait=0.2))
        self.assertIn("демон уже запущен", self.hook_log())

    def test_foreign_pid_counts_as_no_daemon(self) -> None:
        self.running_daemon(["sleep", "30"])
        self.assertEqual(0, self.start()[0])
        self.assertEqual("daemon start", self.record("claude-tts.args"))

    def test_dead_pid_counts_as_absent(self) -> None:
        (self.home / ".claude-tts" / "daemon.pid").write_text("999999999\n")
        self.assertEqual(0, self.start()[0])
        self.assertEqual("daemon start", self.record("claude-tts.args"))

    def test_failed_start_reports_it(self) -> None:
        self.stub_claude_tts(starts_daemon=False)
        self.assertEqual(0, self.start()[0])
        self.assertIn("демон не запустился", self.hook_log())
        self.assertNotIn("демон запущен", self.hook_log())

    def test_player_started_with_tray_and_session_added(self) -> None:
        code, _out, err = self.start(session="u1", cwd="/p/X")
        self.assertEqual(0, code, err)
        self.assertEqual("--tray", self.record("player.args"))
        self.assertIn("запуск плеера отдан", self.hook_log())
        self.assertIn("player.log", self.hook_log())
        self.assertEqual([{"op": "add", "session": "u1", "cwd": "/p/X"}], self.control_requests())
        self.assertIn("сессия добавится при его запуске", self.hook_log())

    def test_hook_does_not_wait_for_player(self) -> None:
        self.player.write_text("#!/bin/bash\nsleep 5\n")
        began = time.monotonic()
        self.assertEqual(0, self.start()[0])
        self.assertLess(time.monotonic() - began, 3)

    def test_player_already_running_is_not_started_again(self) -> None:
        self.hold_player()
        self.assertEqual(0, self.start()[0])
        self.assertIsNone(self.record("player.args", wait=0.3))
        self.assertIn("плеер уже запущен", self.hook_log())
        self.assertIn("сессия передана плееру", self.hook_log())

    def test_mode_not_queue_starts_nothing(self) -> None:
        for mode in ("direct", None):
            with self.subTest(mode=mode):
                self.config_mode(mode)
                self.assertEqual(0, self.start()[0])
                self.assertIn("режим claude-tts не queue", self.hook_log())
        (self.home / ".claude-tts" / "config.json").write_text("{broken")
        self.assertEqual(0, self.start()[0])
        self.assertIsNone(self.record("claude-tts.args", wait=0.2))
        self.assertIsNone(self.record("player.args", wait=0.2))
        self.assertEqual([], self.control_requests())

    def test_event_line_in_log(self) -> None:
        self.start(session="u1", cwd="/p/X")
        first = self.hook_log().splitlines()[0]
        self.assertRegex(first, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d start u1 source=startup reason= cwd=/p/X$")

    def test_missing_claude_tts_exits_1(self) -> None:
        (self.stubs / "claude-tts").unlink()
        with mock.patch.dict(os.environ, {"PATH": str(self.stubs)}):
            code, _out, err = self.start()
        self.assertEqual(1, code)
        self.assertIn("claude-tts", err)

    def test_without_cwd_fails(self) -> None:
        code, _out, err = self.hook("start", {"session_id": "u1"})
        self.assertEqual(1, code)
        self.assertIn("cwd", err)

    def test_without_runtime_dir_fails(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": ""}), \
                mock.patch("tts_player.runtime_dir.platform.system", return_value="Linux"):
            code, _out, err = self.start()
        self.assertEqual(1, code)
        self.assertIn("XDG_RUNTIME_DIR", err)


class EndTest(HookCase):
    def setUp(self) -> None:
        super().setUp()
        self.list_session("u1")  # запрос пишется, только пока сессия в списке
    def test_end_removes_session_and_starts_nothing(self) -> None:
        self.assertEqual(0, self.end(reason="logout")[0])
        self.assertEqual([{"op": "remove", "session": "u1"}], self.control_requests())
        self.assertIsNone(self.record("claude-tts.args", wait=0.2))
        self.assertIsNone(self.record("player.args", wait=0.2))

    def test_end_clear_calls_session_clear_with_cwd(self) -> None:
        self.assertEqual(0, self.end(reason="clear", cwd="/p/X")[0])
        self.assertEqual([{"op": "clear", "session": "u1", "cwd": "/p/X"}], self.control_requests())

    def test_end_clear_without_cwd_fails_and_writes_nothing(self) -> None:
        code, _out, err = self.end(reason="clear")
        self.assertEqual(1, code)
        self.assertIn("cwd", err)
        self.assertEqual([], self.control_requests())

    def test_end_works_without_claude_tts(self) -> None:
        (self.stubs / "claude-tts").unlink()
        with mock.patch.dict(os.environ, {"PATH": str(self.stubs)}):
            self.assertEqual(0, self.end()[0])

    def test_end_ignores_mode(self) -> None:
        self.config_mode("direct")
        self.assertEqual(0, self.end()[0])
        self.assertEqual(1, len(self.control_requests()))

    def test_request_failure_fails_hook(self) -> None:
        with mock.patch("tts_player.session_command.write_request", side_effect=OSError("boom")):
            code, _out, err = self.end()
        self.assertEqual(1, code)
        self.assertIn("session remove не выполнен", err)
        self.assertIn("boom", self.hook_log())


class InputTest(HookCase):
    def test_bad_input_exits_1(self) -> None:
        for raw in ("", "not json", "[]", '{"a": 1}\n{"b": 2}'):
            with self.subTest(raw=raw):
                code, _out, err = self.hook("start", raw)
                self.assertEqual(1, code)
                self.assertIn("JSON", err)
        code, _out, err = self.hook("end", {"reason": "x"})
        self.assertEqual(1, code)
        self.assertIn("session_id", err)

    def test_bad_arguments_print_usage_without_reading_stdin(self) -> None:
        stdin = mock.Mock()
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            for args in ([], ["on"], ["start", "end"]):
                self.assertEqual(1, hook_command.main(args, stdin, self.home, self.player))
        stdin.read.assert_not_called()
        self.assertIn("Usage", err.getvalue())

    def test_bin_dispatches_to_module(self) -> None:
        result = subprocess.run([str(HOOK), "bogus"], capture_output=True, text=True, input="")
        self.assertEqual(1, result.returncode)
        self.assertIn("Usage", result.stderr)


class VoicedTest(HookCase):
    PAYLOAD = {"session_id": "u1", "transcript_path": "/t.jsonl"}

    def test_session_not_listed_is_silent(self) -> None:
        self.hold_player()
        for event in ("stop", "post-tool-use", "pre-tool-use", "prompt"):
            with self.subTest(event=event):
                self.assertEqual((0, "", ""), self.hook(event, self.PAYLOAD))
        self.assertIsNone(self.record("claude-tts.args", wait=0.2))

    def test_player_not_running_is_silent(self) -> None:
        self.list_session("u1")
        self.assertEqual(0, self.hook("stop", self.PAYLOAD)[0])
        self.assertIsNone(self.record("claude-tts.args", wait=0.2))

    def test_without_session_id_or_json_is_silent(self) -> None:
        self.hold_player()
        self.assertEqual(0, self.hook("stop", {"x": 1})[0])
        self.assertEqual(0, self.hook("stop", "not json")[0])
        self.assertIsNone(self.record("claude-tts.args", wait=0.2))

    def test_stop_speaks_into_hook_queue(self) -> None:
        self.hold_player()
        self.list_session("u1")
        self.assertEqual(0, self.hook("stop", self.PAYLOAD)[0])
        self.assertEqual("speak --from-hook --hook-type stop", self.record("claude-tts.args"))
        queue = self.runtime / "tts-player" / "hook-queue"
        self.assertEqual(f"session=u1 queue={queue}", self.record("claude-tts.env"))
        self.assertEqual(self.PAYLOAD, json.loads(self.record("claude-tts.stdin")))

    def test_post_tool_use_hook_type(self) -> None:
        self.hold_player()
        self.list_session("u1")
        self.hook("post-tool-use", self.PAYLOAD)
        self.assertEqual("speak --from-hook --hook-type post_tool_use", self.record("claude-tts.args"))

    def test_pre_tool_use_speaks_as_intermediate(self) -> None:
        """Текст перед вопросом — та же промежуточная речь, что после инструмента."""
        self.hold_player()
        self.list_session("u1")
        self.hook("pre-tool-use", self.PAYLOAD)
        self.assertEqual("speak --from-hook --hook-type post_tool_use", self.record("claude-tts.args"))

    def question_payload(self, transcript: Path) -> dict:
        return {"session_id": "u1", "transcript_path": str(transcript), "tool_name": "AskUserQuestion",
                "tool_use_id": "toolu_Q1", "tool_input": {"questions": [
                    {"question": "Какой вариант?", "options": [{"label": "Первый"}, {"label": "Второй"}]}]}}

    def test_pre_tool_use_waits_for_question_in_transcript(self) -> None:
        """PreToolUse приходит раньше, чем сообщение с вопросом ляжет в transcript (замер
        2026-10-22: +0,2..0,5 с): claude-tts зовётся, когда строка с tool_use_id уже в файле."""
        transcript = self.root / "t.jsonl"
        transcript.write_text('{"type": "user"}\n')
        self.stub_claude_tts(body=f'grep -q toolu_Q1 "{transcript}" && echo "$*" >> "{self.records}/seen"\n')
        self.hold_player()
        self.list_session("u1")
        writer = threading.Timer(0.3, lambda: transcript.open("a").write(
            '{"type": "assistant", "id": "toolu_Q1"}\n'))
        writer.start()
        self.addCleanup(writer.cancel)
        self.assertEqual(0, self.hook("pre-tool-use", self.question_payload(transcript))[0])
        self.assertEqual("speak --from-hook --hook-type post_tool_use", self.record("seen").splitlines()[0])

    def test_pre_tool_use_gives_up_waiting_after_timeout(self) -> None:
        """Строка не появилась за отведённое время — речь всё равно отдаётся claude-tts."""
        transcript = self.root / "t.jsonl"
        transcript.write_text('{"type": "user"}\n')
        self.hold_player()
        self.list_session("u1")
        with mock.patch.object(hook_command, "TOOL_USE_WAIT", 0.2):
            self.assertEqual(0, self.hook("pre-tool-use", self.question_payload(transcript))[0])
        self.assertEqual("speak --from-hook --hook-type post_tool_use",
                         self.record("claude-tts.args").splitlines()[0])

    def test_pre_tool_use_speaks_question_and_options_after_text(self) -> None:
        transcript = self.root / "t.jsonl"
        transcript.write_text('{"id": "toolu_Q1"}\n')
        self.hold_player()
        self.list_session("u1")
        self.hook("pre-tool-use", self.question_payload(transcript))
        self.assertEqual(["speak --from-hook --hook-type post_tool_use",
                          "speak Какой вариант? Варианты: Первый, Второй."],
                         self.record("claude-tts.args").splitlines())
        queue = self.runtime / "tts-player" / "hook-queue"
        self.assertEqual(f"session=u1 queue={queue}", self.record("claude-tts.env"))

    def test_post_tool_use_does_not_speak_question(self) -> None:
        """После ответа вопрос не повторяется: его читает только pre-tool-use."""
        transcript = self.root / "t.jsonl"
        transcript.write_text('{"id": "toolu_Q1"}\n')
        self.hold_player()
        self.list_session("u1")
        self.hook("post-tool-use", self.question_payload(transcript))
        self.assertEqual(["speak --from-hook --hook-type post_tool_use"],
                         self.record("claude-tts.args").splitlines())

    def test_prompt_passes_tone_to_stdout_without_queue(self) -> None:
        """stdout tone-context — контекст запроса: хук отдаёт его как есть."""
        self.stub_claude_tts(body='echo "тон: спокойный"\n')
        self.hold_player()
        self.list_session("u1")
        result = subprocess.run(
            [str(HOOK), "prompt"], input=json.dumps(self.PAYLOAD), capture_output=True, text=True,
            env=os.environ)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("тон: спокойный\n", result.stdout)
        self.assertEqual("handy tone-context", self.record("claude-tts.args"))
        self.assertEqual("session=u1 queue=", self.record("claude-tts.env"))

    def test_command_exit_code_is_hooks(self) -> None:
        self.stub_claude_tts(body="exit 3\n")
        self.hold_player()
        self.list_session("u1")
        self.assertEqual(3, self.hook("stop", self.PAYLOAD)[0])


if __name__ == "__main__":
    unittest.main()
