"""Команда `tts-player session has|add|remove`: гейт хука спрашивает, в списке ли сессия запущенного плеера;
add и remove кладут запрос в control/.
Подпроцесс bin/tts-player с XDG_RUNTIME_DIR во временном каталоге."""
import tests  # noqa: F401 — окружение тестов (tests/__init__.py)
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tts_player import session_command
from tts_player.instance import Instance
from tts_player.sessions import Sessions, sessions_path

BIN = Path(__file__).resolve().parent.parent / "bin" / "tts-player"


def run_cli(runtime: Path | None, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
    if runtime is not None:
        env["XDG_RUNTIME_DIR"] = str(runtime)
    return subprocess.run([str(BIN), *args], env=env, capture_output=True, text=True, timeout=30)


class SessionHasTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runtime = Path(tmp.name)
        self.path = sessions_path(self.runtime)

    def lock_player(self) -> None:
        instance = Instance.acquire(self.runtime)
        self.assertIsNotNone(instance)
        self.addCleanup(instance.release)

    def add(self, session_id: str) -> None:
        Sessions(self.path).add(session_id, "/work/proj")

    def test_listed_and_player_running(self) -> None:
        self.add("u1")
        self.lock_player()
        result = run_cli(self.runtime, "session", "has", "u1")
        self.assertEqual((0, "", ""), (result.returncode, result.stdout, result.stderr))

    def test_not_listed(self) -> None:
        self.add("u1")
        self.lock_player()
        result = run_cli(self.runtime, "session", "has", "u2")
        self.assertEqual((1, "", ""), (result.returncode, result.stdout, result.stderr))

    def test_player_not_running(self) -> None:
        self.add("u1")
        self.assertEqual(1, run_cli(self.runtime, "session", "has", "u1").returncode)

    def test_no_file_creates_nothing(self) -> None:
        self.lock_player()
        before = sorted(p.name for p in self.runtime.rglob("*"))
        self.assertEqual(1, run_cli(self.runtime, "session", "has", "u1").returncode)
        self.assertFalse(self.path.exists())
        self.assertEqual(before, sorted(p.name for p in self.runtime.rglob("*")))

    def test_no_runtime_dir_creates_nothing(self) -> None:
        self.assertEqual(1, run_cli(None, "session", "has", "u1").returncode)

    def test_broken_file_untouched(self) -> None:
        self.lock_player()
        self.path.write_text("{broken", encoding="utf-8")
        result = run_cli(self.runtime, "session", "has", "u1")
        self.assertEqual((1, "", ""), (result.returncode, result.stdout, result.stderr))
        self.assertEqual("{broken", self.path.read_text(encoding="utf-8"))

    def test_usage_errors(self) -> None:
        for args in (("session",), ("session", "has"), ("session", "foo", "u1"),
                     ("session", "has", "u1", "u2")):
            with self.subTest(args=args):
                result = run_cli(self.runtime, *args)
                self.assertEqual(2, result.returncode)
                self.assertIn("usage", result.stderr)
                self.assertEqual("", result.stdout)


class SessionRequestTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runtime = Path(tmp.name)
        self.path = sessions_path(self.runtime)
        self.control = self.runtime / "tts-player" / "control"

    def lock_player(self) -> None:
        instance = Instance.acquire(self.runtime)
        self.assertIsNotNone(instance)
        self.addCleanup(instance.release)

    def requests(self) -> list[dict]:
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.control.glob("*.json"))]

    def assert_no_tmp(self) -> None:
        self.assertEqual([], list(self.control.glob("*.tmp")))

    def test_add_writes_request(self) -> None:
        self.lock_player()
        result = run_cli(self.runtime, "session", "add", "u1", "/p/TTSReader")
        self.assertEqual((0, "сессия передана плееру\n", ""),
                         (result.returncode, result.stdout, result.stderr))
        self.assertEqual([{"op": "add", "session": "u1", "cwd": "/p/TTSReader"}], self.requests())
        self.assert_no_tmp()

    def test_add_already_listed(self) -> None:
        Sessions(self.path).add("u1", "/p/TTSReader")
        self.lock_player()
        result = run_cli(self.runtime, "session", "add", "u1", "/p/TTSReader")
        self.assertEqual((0, "сессия уже в списке\n"), (result.returncode, result.stdout))
        self.assertEqual([], self.requests())

    def test_remove_writes_request(self) -> None:
        Sessions(self.path).add("u1", "/p/TTSReader")
        self.lock_player()
        result = run_cli(self.runtime, "session", "remove", "u1")
        self.assertEqual((0, "сессия передана плееру на удаление\n", ""),
                         (result.returncode, result.stdout, result.stderr))
        self.assertEqual([{"op": "remove", "session": "u1"}], self.requests())
        self.assert_no_tmp()

    def test_remove_not_listed(self) -> None:
        Sessions(self.path).add("u1", "/p/TTSReader")
        self.lock_player()
        result = run_cli(self.runtime, "session", "remove", "u2")
        self.assertEqual((0, "сессия не в списке\n"), (result.returncode, result.stdout))
        self.assertEqual([], self.requests())

    def test_clear_writes_request(self) -> None:
        Sessions(self.path).add("u1", "/p")
        self.lock_player()
        result = run_cli(self.runtime, "session", "clear", "u1", "/p")
        self.assertEqual((0, "сессия передана плееру на удаление\n", ""),
                         (result.returncode, result.stdout, result.stderr))
        self.assertEqual([{"op": "clear", "session": "u1", "cwd": "/p"}], self.requests())
        self.assert_no_tmp()

    def test_clear_not_listed(self) -> None:
        self.lock_player()
        result = run_cli(self.runtime, "session", "clear", "u1", "/p")
        self.assertEqual((0, "сессия не в списке\n"), (result.returncode, result.stdout))
        self.assertEqual([], self.requests())

    def test_remove_after_pending_add_writes_both(self) -> None:
        run_cli(self.runtime, "session", "add", "u1", "/p/X")
        result = run_cli(self.runtime, "session", "remove", "u1")
        self.assertEqual(0, result.returncode)
        self.assertNotIn("не в списке", result.stdout)
        self.assertEqual([{"op": "add", "session": "u1", "cwd": "/p/X"},
                          {"op": "remove", "session": "u1"}], self.requests())

    def test_add_after_pending_remove_writes_both(self) -> None:
        Sessions(self.path).add("u1", "/p/X")
        self.lock_player()
        run_cli(self.runtime, "session", "remove", "u1")
        result = run_cli(self.runtime, "session", "add", "u1", "/p/X")
        self.assertEqual(0, result.returncode)
        self.assertNotIn("уже в списке", result.stdout)
        self.assertEqual([{"op": "remove", "session": "u1"},
                          {"op": "add", "session": "u1", "cwd": "/p/X"}], self.requests())

    def test_player_not_running(self) -> None:
        Sessions(self.path).add("u1", "/p/TTSReader")
        result = run_cli(self.runtime, "session", "add", "u2", "/p/other")
        self.assertEqual((0, "плеер не запущен — сессия добавится при его запуске\n"),
                         (result.returncode, result.stdout))
        result = run_cli(self.runtime, "session", "remove", "u1")
        self.assertEqual((0, "плеер не запущен — сессия уберётся при его запуске\n"),
                         (result.returncode, result.stdout))
        self.assertEqual([{"op": "add", "session": "u2", "cwd": "/p/other"},
                          {"op": "remove", "session": "u1"}], self.requests())
        self.assert_no_tmp()

    def test_broken_sessions_file_is_empty_list(self) -> None:
        self.lock_player()
        self.path.write_text("{broken", encoding="utf-8")
        result = run_cli(self.runtime, "session", "add", "u1", "/p")
        self.assertEqual((0, "сессия передана плееру\n"), (result.returncode, result.stdout))
        self.assertEqual(1, len(self.requests()))

    def test_no_runtime_dir(self) -> None:
        result = run_cli(None, "session", "add", "u1", "/p")
        self.assertEqual(1, result.returncode)
        self.assertNotEqual("", result.stderr)

    def test_write_failure(self) -> None:
        self.lock_player()
        self.control.write_text("не каталог", encoding="utf-8")
        result = run_cli(self.runtime, "session", "add", "u1", "/p")
        self.assertEqual(1, result.returncode)
        self.assertNotEqual("", result.stderr)

    def test_failed_replace_leaves_no_tmp(self) -> None:
        self.lock_player()
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.runtime)}), \
                mock.patch("tts_player.atomic_json.os.replace", side_effect=OSError("сбой")), \
                mock.patch("sys.stderr"):
            code = session_command.main(["add", "u1", "/p"])
        self.assertEqual(1, code)
        self.assertEqual([], list(self.control.iterdir()))

    def test_usage_errors(self) -> None:
        for args in (
            ("session", "add", "u1"),
            ("session", "add"),
            ("session", "add", "u1", "/p", "x"),
            ("session", "remove"),
            ("session", "remove", "u1", "u2"),
            ("session", "clear"),
            ("session", "clear", "u1"),
            ("session", "clear", "u1", "/p", "x"),
        ):
            with self.subTest(args=args):
                result = run_cli(self.runtime, *args)
                self.assertEqual(2, result.returncode)
                self.assertIn("add <uuid> <cwd>", result.stderr)
                self.assertIn("remove <uuid>", result.stderr)
                self.assertIn("clear <uuid> <cwd>", result.stderr)
                self.assertIn("has <uuid>", result.stderr)
                self.assertFalse(self.control.exists())

    def test_module_does_not_import_gi(self) -> None:
        code = "import sys, tts_player.session_command; assert 'gi' not in sys.modules"
        env = {**os.environ, "PYTHONPATH": str(BIN.parent.parent)}
        result = subprocess.run(["python3", "-c", code], env=env, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
