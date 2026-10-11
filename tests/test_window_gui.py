"""GUI-тесты окна (tests/gui_window_cases.py) на бэкенде GTK Broadway.

broadwayd — свой дисплей в памяти с HTTP на 127.0.0.1: окна рисуются туда, а не на экран
пользователя. Подпроцесс тестов идёт без DISPLAY/WAYLAND_DISPLAY, с GDK_BACKEND=broadway
и своим XDG_RUNTIME_DIR (там сокет broadwayd).
"""
import tests  # noqa: F401 — окружение тестов (tests/__init__.py)
import os
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BROADWAYD = shutil.which("broadwayd")
DISPLAY_NUMBER = 7
START_WAIT = 5.0
RUN_WAIT = 60.0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@unittest.skipUnless(BROADWAYD, "нет broadwayd (пакет libgtk-3-bin): GUI-тесты окна не запускаются")
class WindowGuiTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runtime = Path(tmp.name)
        self.runtime.chmod(0o700)
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("DISPLAY", "WAYLAND_DISPLAY", "TTS_PLAYER_GUI_TESTS")}
        self.env.update(XDG_RUNTIME_DIR=str(self.runtime), GDK_BACKEND="broadway",
                        BROADWAY_DISPLAY=f":{DISPLAY_NUMBER}")
        self.start_broadwayd()

    def start_broadwayd(self) -> None:
        """Сокет broadwayd — абстрактный, с путём от XDG_RUNTIME_DIR: в своём каталоге номер
        дисплея не занят. Порт HTTP — свободный. Готовность — строка «Listening on»."""
        broadwayd = subprocess.Popen(
            [BROADWAYD, "--address", "127.0.0.1", "--port", str(free_port()),
             f":{DISPLAY_NUMBER}"],
            env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        self.addCleanup(self.stop, broadwayd)
        ready, _, _ = select.select([broadwayd.stdout], [], [], START_WAIT)
        line = broadwayd.stdout.readline() if ready else ""
        if not line.startswith("Listening on"):
            self.fail(f"broadwayd не готов за {START_WAIT} с: {line!r}")

    @staticmethod
    def stop(proc: subprocess.Popen) -> None:
        proc.terminate()
        try:
            proc.wait(timeout=START_WAIT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        proc.stdout.close()

    def test_window_cases(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.gui_window_cases", "-v"],
            cwd=ROOT, env=self.env, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=RUN_WAIT)
        output = result.stdout + result.stderr
        self.assertEqual(0, result.returncode, output)
        self.assertNotIn("skipped", output, output)
        self.assertIn("OK", output, output)


if __name__ == "__main__":
    unittest.main()
