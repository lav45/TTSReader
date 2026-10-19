"""Приёмка стыка: проигрыватель, которым код демона играет предложение, — `tts-player sink` из
player_command в config.json, а id сообщения приходит в CLAUDE_TTS_MESSAGE_ID."""
import tests  # noqa: F401 — окружение тестов (tests/__init__.py)
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tts_player.claude_tts_python import claude_tts_python

BIN = Path(__file__).resolve().parent.parent / "bin" / "tts-player"

# Вызов как у daemon_play_audio (daemon.py): cmd = detect_player() + [wav];
# Popen(cmd, env=player_env(состояние)).wait(). Сам daemon_play_audio не вызывается: он пишет
# состояние демона в ~/.claude-tts/. argv[2] — id текущего сообщения или "" (его нет).
PLAY_SCRIPT = (
    "import subprocess, sys\n"
    "from claude_code_tts.audio import detect_player\n"
    "from claude_code_tts.daemon import player_env\n"
    "cmd = detect_player()\n"
    "current = {'id': sys.argv[2]} if sys.argv[2] else None\n"
    "env = player_env({'current_message': current})\n"
    "sys.exit(subprocess.Popen(cmd + [sys.argv[1]], env=env).wait())\n"
)


def wav_name(name):
    """Имя WAV демона из имени в inbox/: без <recv_ns>_<id задачи>_ (контракт (13))."""
    return name.split("_", 2)[2]


def inbox_reader(inbox):
    """Читатель-заглушка: имена предложений в порядке приёма, без служебных точечных файлов."""
    return sorted(f for f in os.listdir(inbox) if not f.startswith("."))


class DaemonInterceptionTest(unittest.TestCase):
    def setUp(self):
        self.python = claude_tts_python()
        if self.python is None or not self.python.exists():
            self.skipTest("claude-tts не найден в PATH")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.queue = self.root / "queue"
        self.queue.mkdir()
        self.inbox = self.runtime / "tts-player" / "inbox"
        # HOME — временный: config.json демона с player_command, настоящий не читается
        config = self.root / ".claude-tts" / "config.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"player_command": [str(BIN), "sink"]}))

    def play(self, name, data, task_id=""):
        wav = self.queue / name
        wav.write_bytes(data)
        env = {"PATH": os.environ.get("PATH", ""), "XDG_RUNTIME_DIR": str(self.runtime),
               "HOME": str(self.root)}
        result = subprocess.run([str(self.python), "-I", "-c", PLAY_SCRIPT, str(wav), task_id],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_sentences_reach_reader_in_order_intact_and_with_message_id(self):
        sentences = {f"tts_queue_sess_tok_s{i}.wav": f"RIFF-sentence-{i}-".encode() + bytes([i, 255 - i]) * 7
                     for i in (2, 0, 1)}
        for name, data in sentences.items():
            self.play(name, data, "a1b2c3")
        names = inbox_reader(self.inbox)
        self.assertEqual([wav_name(n) for n in names], list(sentences))
        self.assertTrue(all(n.split("_")[1] == "a1b2c3" for n in names), names)
        for name, data in zip(names, sentences.values()):
            self.assertEqual((self.inbox / name).read_bytes(), data)

    def test_announcement_is_invisible_to_reader(self):
        self.play("tts_queue_sess_tok_s0.wav", b"s0")
        self.play("tts_announce.wav", b"announcement")
        self.play("tts_queue_sess_tok_s1.wav", b"s1")
        names = inbox_reader(self.inbox)
        self.assertEqual([wav_name(n) for n in names],
                         ["tts_queue_sess_tok_s0.wav", "tts_queue_sess_tok_s1.wav"])
        self.assertTrue(all("_unknown_" in n for n in names), names)


if __name__ == "__main__":
    unittest.main()
