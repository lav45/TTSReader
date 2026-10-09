"""Приёмка плеера через стык: daemon-bin/paplay -> inbox -> процесс tts_player -> строки stderr."""
import os
import queue
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
import wave
from pathlib import Path

from test_history import SCHEMA

ROOT = Path(__file__).resolve().parent.parent
PAPLAY = ROOT / "daemon-bin" / "paplay"
WAIT = 10.0
QUIET = 1.0
RATE = 8000
SECONDS = 0.25
PROTOCOL = ("playing ", "error ", "complete ", "ended ", "switched ")


def make_wav(path: Path, seconds: float = SECONDS) -> Path:
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(b"\x00\x00" * int(RATE * seconds))
    return path


class PlayerProcessCase(unittest.TestCase):
    """Процесс плеера во временном окружении: свой XDG_RUNTIME_DIR, своя история демона."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.runtime = self.tmp / "run"
        self.runtime.mkdir()
        self.inbox = self.runtime / "tts-player" / "inbox"
        self.store = self.runtime / "tts-player" / "store"
        self.wavs = self.tmp / "wavs"
        self.wavs.mkdir()
        self.history = self.tmp / "speech_history"
        self.history.mkdir()
        self.db = self.tmp / "handy_analysis.db"
        conn = sqlite3.connect(str(self.db))
        conn.execute(SCHEMA)
        conn.commit()
        conn.close()
        self.lines: queue.Queue[str] = queue.Queue()
        self.proc: subprocess.Popen | None = None

    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
        env["XDG_RUNTIME_DIR"] = str(self.runtime)
        return env

    @staticmethod
    def command(*args: str) -> list[str]:
        """Процесс плеера без окна и трея: e2e не открывают ничего на экране пользователя."""
        return [sys.executable, "-m", "tts_player", "--no-gui", *args]

    def start_player(self, sink: str = "fakesink", history: Path | None = None) -> None:
        history = self.history if history is None else history
        self.proc = subprocess.Popen(
            self.command("--audio-sink", sink,
                         "--history-dir", str(history), "--history-db", str(self.db)),
            cwd=ROOT, env=self.env(), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(self.terminate)
        threading.Thread(target=self.pump, args=(self.proc,), daemon=True).start()

    def terminate(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()

    def pump(self, proc: subprocess.Popen) -> None:
        for line in proc.stderr:
            self.lines.put(line.rstrip("\n"))
        proc.stderr.close()

    def sentence(self, session: str, token: str, index: int, valid: bool = True,
                 seconds: float = SECONDS) -> Path:
        path = self.wavs / f"tts_queue_{session}_{token}_s{index}.wav"
        if valid:
            make_wav(path, seconds)
        else:
            path.write_bytes(b"not a wav at all")
        return path

    def drop(self, path: Path) -> None:
        subprocess.run([str(PAPLAY), str(path)], env=self.env(), check=True)

    def next_line(self, prefix: str) -> str:
        """Ближайшая строка с префиксом; чужие строки пропускаются; таймаут — провал теста."""
        while True:
            try:
                line = self.lines.get(timeout=WAIT)
            except queue.Empty:
                self.fail(f"нет строки '{prefix}…' за {WAIT} с")
            if line.startswith(prefix):
                return line

    def played(self, count: int) -> list[str]:
        return [self.next_line("playing ").removeprefix("playing ") for _ in range(count)]

    def assert_quiet(self, seconds: float = QUIET) -> None:
        """За seconds ни одной строки протокола (PROTOCOL); диагностика INFO/WARNING не в счёт."""
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            try:
                line = self.lines.get(timeout=left)
            except queue.Empty:
                return
            if line.startswith(PROTOCOL):
                self.fail(f"неожиданная строка: {line}")


class PlayerE2ETest(PlayerProcessCase):
    def test_messages_play_in_order(self) -> None:
        session = uuid.uuid4().hex
        self.start_player()
        paths = [self.sentence(session, "aaaaaa", 0), self.sentence(session, "aaaaaa", 1),
                 self.sentence(session, "bbbbbb", 0)]
        for path in paths:
            self.drop(path)
        names = self.played(3)
        self.assertEqual(
            [f"_tts_queue_{session}_aaaaaa_s0.wav", f"_tts_queue_{session}_aaaaaa_s1.wav",
             f"_tts_queue_{session}_bbbbbb_s0.wav"],
            [n[n.index("_tts_queue"):] for n in names])

    def test_late_sentence_of_finished_message_is_not_played(self) -> None:
        session = uuid.uuid4().hex
        self.start_player()
        self.drop(self.sentence(session, "aaaaaa", 0))
        self.played(1)
        self.drop(self.sentence(session, "bbbbbb", 0))
        self.assertTrue(self.next_line("playing ").endswith("bbbbbb_s0.wav"))
        self.drop(self.sentence(session, "aaaaaa", 2))
        self.assert_quiet()
        self.assertIsNone(self.proc.poll())
        self.assertEqual([], [p.name for p in self.inbox.iterdir()])
        self.drop(self.sentence(session, "cccccc", 0))
        self.assertTrue(self.next_line("playing ").endswith("cccccc_s0.wav"))

    def test_backlog_plays_first_by_recv_ns(self) -> None:
        session = uuid.uuid4().hex
        for token in ("aaaaaa", "bbbbbb"):
            self.drop(self.sentence(session, token, 0))
        self.start_player()
        names = self.played(2)
        self.assertTrue(names[0].endswith("aaaaaa_s0.wav"))
        self.assertTrue(names[1].endswith("bbbbbb_s0.wav"))

    def test_broken_wav_reports_error_and_next_still_plays(self) -> None:
        session = uuid.uuid4().hex
        self.start_player()
        self.drop(self.sentence(session, "aaaaaa", 0, valid=False))
        error = self.next_line("error ")
        self.assertTrue(error.startswith("error ") and "aaaaaa_s0.wav: " in error, error)
        self.drop(self.sentence(session, "bbbbbb", 0))
        self.assertTrue(self.next_line("playing ").endswith("bbbbbb_s0.wav"))

    def test_sigterm_returns_unfinished_to_inbox_and_restart_plays_them(self) -> None:
        session = uuid.uuid4().hex
        self.drop(self.sentence(session, "aaaaaa", 0, seconds=3.0))
        self.drop(self.sentence(session, "aaaaaa", 1))
        self.drop(self.sentence(session, "bbbbbb", 0))
        self.start_player()
        self.assertTrue(self.played(1)[0].endswith("aaaaaa_s0.wav"))
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(0, self.proc.wait(timeout=WAIT))
        back = sorted(p.name for p in self.inbox.iterdir())
        self.assertEqual(
            [f"_tts_queue_{session}_aaaaaa_s0.wav", f"_tts_queue_{session}_aaaaaa_s1.wav",
             f"_tts_queue_{session}_bbbbbb_s0.wav"],
            [n[n.index("_tts_queue"):] for n in back])
        self.assertEqual([], list(self.store.iterdir()))
        self.start_player()
        self.assertEqual(back, self.played(3))

    def test_output_error_keeps_sentence_and_reports_once(self) -> None:
        session = uuid.uuid4().hex
        self.start_player(sink="filesink")  # filesink без location не открывается — звука нет
        self.drop(self.sentence(session, "aaaaaa", 0))
        self.drop(self.sentence(session, "bbbbbb", 0))
        self.assertTrue(self.next_line("playing ").endswith("aaaaaa_s0.wav"))
        error = self.next_line("error ")
        self.assertIn("aaaaaa_s0.wav: ", error)
        self.assert_quiet(4.5)  # два повтора по 2 с — ни новой строки error, ни перехода к bbbbbb
        self.assertIsNone(self.proc.poll())
        self.assertEqual(2, len(list(self.store.iterdir())))

    def test_without_runtime_dir_exits_1_with_message(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
        result = subprocess.run(
            self.command("--audio-sink", "fakesink"),
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=WAIT)
        self.assertEqual(1, result.returncode)
        self.assertIn("XDG_RUNTIME_DIR", result.stderr)

    def test_gui_without_display_exits_1_with_message(self) -> None:
        """Без --no-gui и без графической сессии: окно открыть негде — выход 1."""
        env = {k: v for k, v in self.env().items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        result = subprocess.run(
            [sys.executable, "-m", "tts_player", "--audio-sink", "fakesink",
             "--history-dir", str(self.history), "--history-db", str(self.db)],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=WAIT)
        self.assertEqual(1, result.returncode)
        self.assertIn("графическ", result.stderr)
        self.assertFalse(self.inbox.exists())

    def test_sigterm_exits_0(self) -> None:
        self.start_player()
        self.drop(self.sentence(uuid.uuid4().hex, "aaaaaa", 0))
        self.played(1)
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(0, self.proc.wait(timeout=WAIT))


if __name__ == "__main__":
    unittest.main()
