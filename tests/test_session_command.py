"""Команда `tts-player session has <uuid>`: гейт хука спрашивает, в списке ли сессия запущенного плеера.
Подпроцесс bin/tts-player с XDG_RUNTIME_DIR во временном каталоге."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
