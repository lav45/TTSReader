"""Тесты synth_worker: процесс синтеза с поддельным пакетом claude_code_tts (реальный синтез не запускается)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

WORKER = Path(__file__).resolve().parent.parent / "tts_player" / "synth_worker.py"

FAKE_BRIDGE = '''
def split_sentences(text):
    return [p.strip() + "." for p in text.split(".") if p.strip()]
'''

FAKE_CONFIG = '''
import os


def load_raw_config():
    mode = os.environ.get("FAKE_CONFIG_MODE", "")
    if mode == "raise":
        raise OSError("config broken")
    if mode == "missing":
        return {}
    if mode == "empty":
        return {"active_persona": ""}
    if mode == "list":
        return []
    return {"active_persona": "fake-persona"}
'''

FAKE_DAEMON = '''
import os
import time
import wave


def daemon_generate_speech(text, persona, output_file):
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(persona + "|" + text + "\\n")
    if os.environ.get("FAKE_NOISE"):
        print("noise")
    time.sleep(float(os.environ.get("FAKE_DELAY", "0")))
    fail_at = os.environ.get("FAKE_FAIL_AT", "")
    if os.environ.get("FAKE_BAD_WAV"):
        output_file.write_bytes(b"not a wav")
        return True
    if fail_at and text == fail_at:
        output_file.write_bytes(b"partial")
        if os.environ.get("FAKE_FAIL_MODE") == "raise":
            raise RuntimeError("boom")
        return False
    with wave.open(str(output_file), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\\x00\\x00" * 4000)
    return True
'''


class SynthWorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        pkg = root / "fake" / "claude_code_tts"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "bridge.py").write_text(FAKE_BRIDGE)
        (pkg / "config.py").write_text(FAKE_CONFIG)
        (pkg / "daemon.py").write_text(FAKE_DAEMON)
        self.fake_path = str(root / "fake")
        self.out = root / "out" / "nested"
        self.log = root / "calls.log"

    def env(self, with_package=True, **extra):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        if with_package:
            env["PYTHONPATH"] = self.fake_path
        env["FAKE_LOG"] = str(self.log)
        env.update(extra)
        return env

    def start(self, **kwargs):
        return subprocess.Popen(
            [sys.executable, str(WORKER), str(self.out)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=self.env(**kwargs),
        )

    def run_worker(self, text, **kwargs):
        proc = self.start(**kwargs)
        stdout, _ = proc.communicate(text, timeout=30)
        lines = [json.loads(line) for line in stdout.splitlines()]
        return proc.returncode, lines

    def test_three_sentences(self):
        code, lines = self.run_worker("Раз. Два. Три.")
        self.assertEqual(code, 0)
        self.assertEqual(lines[0], {"count": 3})
        for i in range(3):
            line = lines[1 + i]
            self.assertEqual(line["index"], i)
            self.assertEqual(line["path"], str(self.out / f"{i}.wav"))
            self.assertTrue(Path(line["path"]).exists())
            self.assertAlmostEqual(line["duration"], 0.5, places=3)
        self.assertEqual(lines[4], {"done": True})
        self.assertEqual(len(lines), 5)

    def test_persona_from_config(self):
        self.run_worker("Раз. Два.")
        calls = self.log.read_text().splitlines()
        self.assertEqual(calls, ["fake-persona|Раз.", "fake-persona|Два."])

    def test_failure_on_second_sentence(self):
        code, lines = self.run_worker("Раз. Два. Три.", FAKE_FAIL_AT="Два.")
        self.assertEqual(code, 1)
        self.assertEqual([line.get("index") for line in lines[:2]], [None, 0])
        self.assertIn("error", lines[-1])
        self.assertFalse(any(line.get("index") == 2 for line in lines))
        self.assertFalse(any(line.get("done") for line in lines))
        self.assertEqual(len(self.log.read_text().splitlines()), 2)

    def test_exception_on_synthesis(self):
        code, lines = self.run_worker("Раз. Два.", FAKE_FAIL_AT="Раз.", FAKE_FAIL_MODE="raise")
        self.assertEqual(code, 1)
        self.assertIn("boom", lines[-1]["error"])

    def test_persona_resolution_failures(self):
        for mode in ("raise", "missing", "empty", "list"):
            with self.subTest(mode=mode):
                code, lines = self.run_worker("Раз.", FAKE_CONFIG_MODE=mode)
                self.assertEqual(code, 1)
                self.assertIn("error", lines[-1])
                self.assertFalse(any(line.get("done") for line in lines))

    def test_stdout_is_pure_protocol(self):
        proc = self.start(FAKE_NOISE="1")
        stdout, stderr = proc.communicate("Раз. Два.", timeout=30)
        self.assertEqual(proc.returncode, 0)
        lines = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual(len(lines), 4)
        self.assertEqual(stderr.count("noise"), 2)

    def test_part_name_never_in_stdout_and_failure_leaves_no_wav(self):
        code, lines = self.run_worker("Раз. Два.", FAKE_FAIL_AT="Два.")
        self.assertEqual(code, 1)
        self.assertNotIn(".part", json.dumps(lines))
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["0.wav"])

    def test_bad_wav_gives_error_line(self):
        code, lines = self.run_worker("Раз.", FAKE_BAD_WAV="1")
        self.assertEqual(code, 1)
        self.assertIn("error", lines[-1])
        self.assertEqual(list(self.out.iterdir()), [])

    def test_unwritable_out_dir_gives_error_line(self):
        blocker = Path(self.tmp.name) / "blocker"
        blocker.write_text("file")
        self.out = blocker / "sub"
        code, lines = self.run_worker("Раз.")
        self.assertEqual(code, 1)
        self.assertIn("error", lines[-1])

    def test_import_crash_gives_exit_2(self):
        (Path(self.fake_path) / "claude_code_tts" / "bridge.py").write_text("raise RuntimeError('x')")
        code, lines = self.run_worker("Раз.")
        self.assertEqual(code, 2)
        self.assertIn("error", lines[0])

    def test_missing_package(self):
        code, lines = self.run_worker("Раз.", with_package=False)
        self.assertEqual(code, 2)
        self.assertEqual(len(lines), 1)
        self.assertIn("error", lines[0])

    def test_empty_text(self):
        for text in ("", "  \n\t "):
            with self.subTest(text=text):
                code, lines = self.run_worker(text)
                self.assertEqual(code, 0)
                self.assertEqual(lines, [{"count": 0}, {"done": True}])

    def test_first_path_arrives_before_last_synthesized(self):
        delay = 0.6
        proc = self.start(FAKE_DELAY=str(delay))
        proc.stdin.write("Раз. Два. Три.")
        proc.stdin.close()
        started = time.monotonic()
        count = json.loads(proc.stdout.readline())
        first = json.loads(proc.stdout.readline())
        elapsed = time.monotonic() - started
        self.assertEqual(count, {"count": 3})
        self.assertEqual(first["index"], 0)
        self.assertLess(elapsed, delay * 2)
        proc.stdout.read()
        proc.wait(timeout=30)
        proc.stdout.close()
        proc.stderr.close()
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
