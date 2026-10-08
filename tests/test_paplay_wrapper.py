"""Тесты обёртки daemon-bin/paplay: предложение уходит в буфер плеера, звук не играет."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

WRAPPER = Path(__file__).resolve().parent.parent / "daemon-bin" / "paplay"
WAV_BYTES = b"RIFF\x00\x01fake-wav-\xff\xfe\x00bytes"


class PaplayWrapperTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.src = self.root / "src"
        self.src.mkdir()
        self.inbox = self.runtime / "tts-player" / "inbox"
        # Заглушка logger первой в PATH: пишет аргументы в файл, настоящий journal не трогаем.
        self.logged = self.root / "logger.args"
        stubs = self.root / "stubs"
        stubs.mkdir()
        stub = stubs / "logger"
        stub.write_text(f'#!/bin/bash\necho "$*" >> "{self.logged}"\n')
        stub.chmod(0o755)
        self.stubs = stubs

    def make_wav(self, name="tts_queue_S_T_s0.wav", data=WAV_BYTES):
        path = self.src / name
        path.write_bytes(data)
        return path

    def run_wrapper(self, *args, with_runtime=True):
        env = {"PATH": f"{self.stubs}:{os.environ.get('PATH', '')}"}
        if with_runtime:
            env["XDG_RUNTIME_DIR"] = str(self.runtime)
        return subprocess.run([str(WRAPPER), *map(str, args)], env=env,
                              capture_output=True, text=True)

    def inbox_names(self):
        return sorted(p.name for p in self.inbox.iterdir()) if self.inbox.exists() else []

    def assert_logged(self, stderr):
        """Сообщение сбоя ушло и в stderr, и в logger -t tts-player-paplay (заглушка)."""
        self.assertTrue(self.logged.exists(), "logger не вызван")
        line = self.logged.read_text().strip()
        self.assertTrue(line.startswith("-t tts-player-paplay "), line)
        message = line[len("-t tts-player-paplay "):]
        self.assertIn(message, stderr)

    def test_copies_wav_into_inbox(self):
        wav = self.make_wav()
        result = self.run_wrapper(wav)
        self.assertEqual(result.returncode, 0, result.stderr)
        names = self.inbox_names()
        self.assertEqual(len(names), 1)
        self.assertRegex(names[0], r"^\d{19}_tts_queue_S_T_s0\.wav$")
        self.assertEqual((self.inbox / names[0]).read_bytes(), WAV_BYTES)

    def test_no_dotfiles_left_after_success(self):
        self.run_wrapper(self.make_wav())
        self.assertTrue(self.inbox.is_dir())
        self.assertEqual([n for n in self.inbox_names() if n.startswith(".")], [])

    def test_options_before_file_are_ignored(self):
        wav = self.make_wav("x.wav")
        result = self.run_wrapper("--volume=1", wav)
        self.assertEqual(result.returncode, 0, result.stderr)
        names = self.inbox_names()
        self.assertEqual(len(names), 1)
        self.assertRegex(names[0], r"^\d{19}_x\.wav$")

    def test_names_sort_in_call_order(self):
        wavs = [self.make_wav(f"s{i}.wav") for i in (2, 0, 1)]
        for wav in wavs:
            self.assertEqual(self.run_wrapper(wav).returncode, 0)
        names = self.inbox_names()
        self.assertEqual([n.split("_", 1)[1] for n in names], ["s2.wav", "s0.wav", "s1.wav"])

    def test_announcements_are_skipped(self):
        for name in ("tts_announce.wav", "tts_daemon_announce.wav"):
            result = self.run_wrapper(self.make_wav(name))
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.inbox_names(), [])

    def test_missing_source_fails_and_leaves_inbox_clean(self):
        result = self.run_wrapper(self.src / "nope.wav")
        self.assertEqual(result.returncode, 1)
        self.assertNotEqual(result.stderr.strip(), "")
        self.assertEqual(self.inbox_names(), [])
        self.assert_logged(result.stderr)

    def test_missing_xdg_runtime_dir_fails(self):
        result = self.run_wrapper(self.make_wav(), with_runtime=False)
        self.assertEqual(result.returncode, 1)
        self.assertNotEqual(result.stderr.strip(), "")
        self.assert_logged(result.stderr)

    def test_no_arguments_fails(self):
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 1)
        self.assertNotEqual(result.stderr.strip(), "")
        self.assert_logged(result.stderr)

    def test_unwritable_runtime_dir_fails(self):
        self.runtime.chmod(0o500)
        self.addCleanup(self.runtime.chmod, 0o700)
        result = self.run_wrapper(self.make_wav())
        self.assertEqual(result.returncode, 1)
        self.assertNotEqual(result.stderr.strip(), "")
        self.assert_logged(result.stderr)

    def test_success_does_not_log(self):
        self.run_wrapper(self.make_wav())
        self.assertFalse(self.logged.exists())

    def test_source_wav_stays_in_place(self):
        wav = self.make_wav()
        self.run_wrapper(wav)
        self.assertEqual(wav.read_bytes(), WAV_BYTES)


if __name__ == "__main__":
    unittest.main()
