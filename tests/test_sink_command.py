"""Тесты приёмника `tts-player sink`: предложение уходит в буфер плеера, звук не играет."""
import tests  # noqa: F401 — окружение тестов (tests/__init__.py)
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tts_player import sink_command

BIN = Path(__file__).resolve().parent.parent / "bin" / "tts-player"
WAV_BYTES = b"RIFF\x00\x01fake-wav-\xff\xfe\x00bytes"


class SinkCommandTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.src = self.root / "src"
        self.src.mkdir()
        self.inbox = self.runtime / "tts-player" / "inbox"
        # syslog — заглушка: настоящий журнал не трогаем
        patcher = mock.patch.object(sink_command, "syslog")
        self.syslog = patcher.start()
        self.addCleanup(patcher.stop)

    def make_wav(self, name="tts_queue_S_T_s0.wav", data=WAV_BYTES):
        path = self.src / name
        path.write_bytes(data)
        return path

    def run_sink(self, *args, task_id=None, with_runtime=True):
        env = {k: v for k, v in os.environ.items()
               if k not in ("XDG_RUNTIME_DIR", "CLAUDE_TTS_MESSAGE_ID")}
        if with_runtime:
            env["XDG_RUNTIME_DIR"] = str(self.runtime)
        if task_id is not None:
            env["CLAUDE_TTS_MESSAGE_ID"] = task_id
        with mock.patch.dict(os.environ, env, clear=True):
            return sink_command.main([str(a) for a in args])

    def inbox_names(self):
        return sorted(p.name for p in self.inbox.iterdir()) if self.inbox.exists() else []

    def inbox_clear(self):
        for p in self.inbox.glob("*") if self.inbox.exists() else []:
            p.unlink()

    def assert_logged(self):
        """Сообщение сбоя ушло в syslog под меткой tts-player-sink."""
        self.syslog.openlog.assert_called_with("tts-player-sink")
        self.assertTrue(self.syslog.syslog.called)

    def assert_single_name(self, expected_id, task_id=None):
        self.assertEqual(0, self.run_sink(self.make_wav("tts_queue_S_tok_s0.wav"), task_id=task_id))
        names = self.inbox_names()
        self.assertEqual(len(names), 1)
        self.assertRegex(names[0], rf"^\d{{19}}_{expected_id}_tts_queue_S_tok_s0\.wav$")

    def test_copies_wav_into_inbox(self):
        self.assertEqual(0, self.run_sink(self.make_wav()))
        names = self.inbox_names()
        self.assertEqual(len(names), 1)
        self.assertRegex(names[0], r"^\d{19}_unknown_tts_queue_S_T_s0\.wav$")
        self.assertEqual((self.inbox / names[0]).read_bytes(), WAV_BYTES)

    def test_task_id_from_environment(self):
        self.assert_single_name("a1b2c3d4e5f60718", task_id="a1b2c3d4e5f60718")

    def test_unknown_without_task_id(self):
        self.assert_single_name("unknown")

    def test_unknown_when_id_has_bad_characters(self):
        for bad in ("../evil", "ABC123", "a b", "", "g1", "a" * 65):
            with self.subTest(bad=bad):
                self.inbox_clear()
                self.assert_single_name("unknown", task_id=bad)

    def test_no_dotfiles_left_after_success(self):
        self.run_sink(self.make_wav())
        self.assertEqual([n for n in self.inbox_names() if n.startswith(".")], [])

    def test_options_before_file_are_ignored(self):
        """afplay -r <скорость>: так демон зовёт проигрыватель на macOS."""
        self.assertEqual(0, self.run_sink("-r", "1.5", self.make_wav("x.wav")))
        [name] = self.inbox_names()
        self.assertRegex(name, r"^\d{19}_unknown_x\.wav$")

    def test_names_sort_in_call_order(self):
        for wav in [self.make_wav(f"s{i}.wav") for i in (2, 0, 1)]:
            self.assertEqual(0, self.run_sink(wav))
        self.assertEqual([n.split("_", 2)[2] for n in self.inbox_names()],
                         ["s2.wav", "s0.wav", "s1.wav"])

    def test_announcements_are_skipped(self):
        for name in ("tts_announce.wav", "tts_daemon_announce.wav"):
            self.assertEqual(0, self.run_sink(self.make_wav(name)))
        self.assertEqual(self.inbox_names(), [])

    def test_missing_source_fails_and_leaves_inbox_clean(self):
        self.assertEqual(1, self.run_sink(self.src / "nope.wav"))
        self.assertEqual(self.inbox_names(), [])
        self.assert_logged()

    def test_missing_runtime_dir_fails(self):
        with mock.patch("tts_player.runtime_dir.platform.system", return_value="Linux"):
            self.assertEqual(1, self.run_sink(self.make_wav(), with_runtime=False))
        self.assert_logged()

    def test_no_arguments_fails(self):
        self.assertEqual(1, self.run_sink())
        self.assert_logged()

    def test_unwritable_runtime_dir_fails(self):
        self.runtime.chmod(0o500)
        self.addCleanup(self.runtime.chmod, 0o700)
        self.assertEqual(1, self.run_sink(self.make_wav()))
        self.assert_logged()

    def test_success_does_not_log(self):
        self.run_sink(self.make_wav())
        self.assertFalse(self.syslog.syslog.called)

    def test_source_wav_stays_in_place(self):
        wav = self.make_wav()
        self.run_sink(wav)
        self.assertEqual(wav.read_bytes(), WAV_BYTES)

    def test_bin_dispatches_sink(self):
        """bin/tts-player sink — тот же приёмник, как его зовёт демон (player_command)."""
        env = {**os.environ, "XDG_RUNTIME_DIR": str(self.runtime), "CLAUDE_TTS_MESSAGE_ID": "abc"}
        result = subprocess.run([str(BIN), "sink", str(self.make_wav())], env=env,
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        [name] = self.inbox_names()
        self.assertRegex(name, r"^\d{19}_abc_tts_queue_S_T_s0\.wav$")


if __name__ == "__main__":
    unittest.main()
