"""Каталог состояния: $XDG_RUNTIME_DIR, на macOS без него — $TMPDIR."""
import tests  # noqa: F401 — окружение тестов (tests/__init__.py)
import unittest
from pathlib import Path

from tts_player.runtime_dir import hook_queue_dir, inbox_dir, runtime_dir


class RuntimeDirTest(unittest.TestCase):
    def test_xdg_runtime_dir_wins_everywhere(self) -> None:
        env = {"XDG_RUNTIME_DIR": "/run/user/1000", "TMPDIR": "/var/folders/x/T/"}
        self.assertEqual(Path("/run/user/1000"), runtime_dir(env, "Linux"))
        self.assertEqual(Path("/run/user/1000"), runtime_dir(env, "Darwin"))

    def test_macos_falls_back_to_tmpdir(self) -> None:
        self.assertEqual(Path("/var/folders/x/T"), runtime_dir({"TMPDIR": "/var/folders/x/T/"}, "Darwin"))

    def test_linux_without_xdg_runtime_dir_has_none(self) -> None:
        self.assertIsNone(runtime_dir({"TMPDIR": "/tmp"}, "Linux"))
        self.assertIsNone(runtime_dir({"XDG_RUNTIME_DIR": ""}, "Darwin"))

    def test_shared_paths(self) -> None:
        self.assertEqual(Path("/r/tts-player/inbox"), inbox_dir(Path("/r")))
        self.assertEqual(Path("/r/tts-player/hook-queue"), hook_queue_dir(Path("/r")))


if __name__ == "__main__":
    unittest.main()
