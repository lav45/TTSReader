"""Voicing (tts_player.voicing): флаг озвучки и запуск хука /tts-off на временных файлах."""
import tempfile
import time
import unittest
from pathlib import Path

from gi.repository import GLib

from tts_player.voicing import Voicing

SESSION = "0b1c2d3e-aaaa-bbbb-cccc-1234567890ab"


class VoicingTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.flag = self.dir / "claude-tts.on"
        self.hook = self.dir / "tts-toggle.sh"
        self.out = self.dir / "out.txt"

    def write_hook(self, sleep: str = "0", code: int = 0) -> None:
        self.hook.write_text(
            "#!/bin/bash\n"
            f"printf '%s|%s' \"$1\" \"$CLAUDE_CODE_SESSION_ID\" > {self.out}\n"
            f"sleep {sleep}\n"
            f"exit {code}\n"
        )
        self.hook.chmod(0o755)

    def spin(self, until, timeout: float = 5.0) -> None:
        context = GLib.MainContext.default()
        deadline = time.monotonic() + timeout
        while not until() and time.monotonic() < deadline:
            context.iteration(False)
            time.sleep(0.01)

    def test_is_on_false_without_flag(self) -> None:
        self.assertFalse(Voicing(self.flag, self.hook).is_on())

    def test_is_on_false_with_empty_flag(self) -> None:
        self.flag.write_text(" \n")
        self.assertFalse(Voicing(self.flag, self.hook).is_on())

    def test_is_on_true_with_session(self) -> None:
        self.flag.write_text(SESSION)
        self.assertTrue(Voicing(self.flag, self.hook).is_on())

    def test_turn_off_runs_hook_with_session(self) -> None:
        self.write_hook()
        self.flag.write_text(SESSION + "\n")
        Voicing(self.flag, self.hook).turn_off()
        self.spin(self.out.exists)
        self.spin(lambda: self.out.exists() and self.out.read_text() != "")
        self.assertEqual(f"off|{SESSION}", self.out.read_text())

    def test_turn_off_does_not_wait_for_hook(self) -> None:
        self.write_hook(sleep="1")
        self.flag.write_text(SESSION)
        started = time.monotonic()
        Voicing(self.flag, self.hook).turn_off()
        self.assertLess(time.monotonic() - started, 0.5)
        # дождаться хука, иначе удаление каталога ломает его и сбой попадает в лог соседнего теста
        self.spin(lambda: False, timeout=1.3)

    def test_turn_off_without_flag_skips_hook(self) -> None:
        self.write_hook()
        with self.assertLogs("tts_player.voicing", "INFO") as logs:
            Voicing(self.flag, self.hook).turn_off()
        self.assertIn("уже выключена", logs.output[0])
        self.spin(self.out.exists, timeout=0.3)
        self.assertFalse(self.out.exists())

    def test_turn_off_missing_hook_logs_reason(self) -> None:
        self.flag.write_text(SESSION)
        with self.assertLogs("tts_player.voicing", "WARNING") as logs:
            Voicing(self.flag, self.hook).turn_off()
        self.assertIn(str(self.hook), logs.output[0])

    def test_turn_off_not_executable_logs_reason(self) -> None:
        self.write_hook()
        self.hook.chmod(0o644)
        self.flag.write_text(SESSION)
        with self.assertLogs("tts_player.voicing", "WARNING") as logs:
            Voicing(self.flag, self.hook).turn_off()
        self.assertIn("Permission", logs.output[0])

    def test_turn_off_logs_nonzero_exit_code(self) -> None:
        self.write_hook(code=3)
        self.flag.write_text(SESSION)
        with self.assertLogs("tts_player.voicing", "WARNING") as logs:
            Voicing(self.flag, self.hook).turn_off()
            self.spin(lambda: bool(logs.output))
        self.assertIn("3", logs.output[0])


if __name__ == "__main__":
    unittest.main()
