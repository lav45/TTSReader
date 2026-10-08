"""Приёмка стыка: плеер, найденный кодом демона через PATH, — наша обёртка daemon-bin/paplay."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

DAEMON_BIN = Path(__file__).resolve().parent.parent / "daemon-bin"
DAEMON_PYTHON = Path.home() / ".local/share/uv/tools/claude-code-tts/bin/python"

# Вызов как у daemon_play_audio (daemon.py): cmd = detect_player() + [wav]; Popen(cmd).wait().
# Сам daemon_play_audio не вызывается: он пишет состояние живого демона в ~/.claude-tts/.
WHICH_SCRIPT = (
    "import shutil\n"
    "from claude_code_tts.audio import detect_player\n"
    "print(shutil.which(detect_player()[0]))\n"
)
# Перед Popen проверяем, что player из PATH — ожидаемая обёртка (argv[2]); иначе exit 3 без
# запуска, чтобы системный paplay не дошёл до живого PulseAudio.
PLAY_SCRIPT = (
    "import shutil, subprocess, sys\n"
    "from claude_code_tts.audio import detect_player\n"
    "cmd = detect_player()\n"
    "found = shutil.which(cmd[0])\n"
    "if found != sys.argv[2]:\n"
    "    print(f'player {found!r} is not the wrapper {sys.argv[2]!r}', file=sys.stderr)\n"
    "    sys.exit(3)\n"
    "sys.exit(subprocess.Popen(cmd + [sys.argv[1]]).wait())\n"
)


def inbox_reader(inbox):
    """Читатель-заглушка: имена предложений в порядке приёма, без служебных точечных файлов."""
    return sorted(f for f in os.listdir(inbox) if not f.startswith("."))


class DaemonInterceptionTest(unittest.TestCase):
    def setUp(self):
        if not DAEMON_PYTHON.exists():
            self.skipTest(f"Python демона не найден: {DAEMON_PYTHON}")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.queue = self.root / "queue"
        self.queue.mkdir()
        self.inbox = self.runtime / "tts-player" / "inbox"
        self.path = f"{DAEMON_BIN}:{os.environ.get('PATH', '')}"

    def daemon_python(self, script, *args, path=None):
        env = {"PATH": path or self.path, "XDG_RUNTIME_DIR": str(self.runtime)}
        return subprocess.run([str(DAEMON_PYTHON), "-I", "-c", script, *map(str, args)],
                              env=env, capture_output=True, text=True)

    def run_play(self, wav, path=None):
        return self.daemon_python(PLAY_SCRIPT, wav, DAEMON_BIN / "paplay", path=path)

    def play(self, name, data):
        wav = self.queue / name
        wav.write_bytes(data)
        result = self.run_play(wav)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_play_script_refuses_foreign_player(self):
        wav = self.queue / "tts_queue_sess_tok_s0.wav"
        wav.write_bytes(b"s0")
        result = self.run_play(wav, path=os.environ.get("PATH", ""))
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("is not the wrapper", result.stderr)
        self.assertFalse(self.inbox.exists())

    def test_player_found_by_daemon_is_our_wrapper(self):
        result = self.daemon_python(WHICH_SCRIPT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(DAEMON_BIN / "paplay"))

    def test_sentences_reach_reader_in_order_and_intact(self):
        sentences = {f"tts_queue_sess_tok_s{i}.wav": f"RIFF-sentence-{i}-".encode() + bytes([i, 255 - i]) * 7
                     for i in (2, 0, 1)}
        for i, (name, data) in enumerate(sentences.items()):
            self.play(name, data)
            if i == 0:
                self.play("tts_announce.wav", b"announcement")
        names = inbox_reader(self.inbox)
        self.assertEqual([n.split("_", 1)[1] for n in names], list(sentences))
        for name, data in zip(names, sentences.values()):
            self.assertEqual((self.inbox / name).read_bytes(), data)

    def test_announcement_is_invisible_to_reader(self):
        self.play("tts_queue_sess_tok_s0.wav", b"s0")
        self.play("tts_announce.wav", b"announcement")
        self.play("tts_queue_sess_tok_s1.wav", b"s1")
        names = inbox_reader(self.inbox)
        self.assertEqual([n.split("_", 1)[1] for n in names],
                         ["tts_queue_sess_tok_s0.wav", "tts_queue_sess_tok_s1.wav"])


if __name__ == "__main__":
    unittest.main()
