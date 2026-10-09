"""Приёмка плеера через стык: daemon-bin/paplay -> inbox -> процесс tts_player -> строки stderr."""
import json
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

from test_ducking import STUB as PACTL_STUB, sink_input
from test_history import SCHEMA

ROOT = Path(__file__).resolve().parent.parent
PAPLAY = ROOT / "daemon-bin" / "paplay"
WAIT = 10.0
QUIET = 1.0
RATE = 8000
SECONDS = 0.25
PROTOCOL = ("playing ", "error ", "complete ", "ended ", "switched ")
FOREIGN = sink_input(7, 4242, [60000, 50000])  # чужой поток в выдаче заглушки pactl


def make_wav(path: Path, seconds: float = SECONDS) -> Path:
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(b"\x00\x00" * int(RATE * seconds))
    return path


class PlayerProcessCase(unittest.TestCase):
    """Процесс плеера во временном окружении: свой XDG_RUNTIME_DIR, свой XDG_CONFIG_HOME
    (настройки плеера пользователя не читаются), своя история демона, заглушка pactl первой
    в PATH (громкость настоящих потоков пользователя не меняется ни в одном e2e)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.runtime = self.tmp / "run"
        self.runtime.mkdir()
        self.config = self.tmp / "config"
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
        self.pactl_bin = self.tmp / "bin"
        self.pactl_bin.mkdir()
        stub = self.pactl_bin / "pactl"
        stub.write_text(PACTL_STUB)
        stub.chmod(0o755)
        self.pactl_log = self.tmp / "pactl.log"
        self.pactl_fixture = self.tmp / "sink-inputs.json"
        self.pactl_fixture.write_text(json.dumps([FOREIGN]))

    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
        env["XDG_RUNTIME_DIR"] = str(self.runtime)
        env["XDG_CONFIG_HOME"] = str(self.config)
        env["PATH"] = f"{self.pactl_bin}{os.pathsep}{env.get('PATH', '')}"
        env["PACTL_LOG"] = str(self.pactl_log)
        env["PACTL_FIXTURE"] = str(self.pactl_fixture)
        for name in ("PACTL_SLEEP", "PACTL_EXIT"):
            env.pop(name, None)
        return env

    def volume_sets(self) -> list[list[str]]:
        """Вызовы `pactl set-sink-input-volume` из журнала заглушки, по порядку."""
        if not self.pactl_log.exists():
            return []
        argvs = [line.split("|", 1)[1].split() for line in self.pactl_log.read_text().splitlines()]
        return [argv for argv in argvs if argv[:1] == ["set-sink-input-volume"]]

    def wait_volume_sets(self, count: int) -> list[list[str]]:
        """Ждёт count вызовов set-sink-input-volume; таймаут — провал теста."""
        deadline = time.monotonic() + WAIT
        while len(sets := self.volume_sets()) < count:
            if time.monotonic() > deadline:
                self.fail(f"нет {count} вызовов set-sink-input-volume за {WAIT} с: {sets}")
            time.sleep(0.05)
        return sets

    @staticmethod
    def command(*args: str) -> list[str]:
        """Процесс плеера без окна и трея: e2e не открывают ничего на экране пользователя."""
        return [sys.executable, "-m", "tts_player", "--no-gui", *args]

    def start_player(self, sink: str = "fakesink", history: Path | None = None,
                     *extra: str) -> None:
        history = self.history if history is None else history
        self.proc = subprocess.Popen(
            self.command("--audio-sink", sink,
                         "--history-dir", str(history), "--history-db", str(self.db), *extra),
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
        env = {k: v for k, v in self.env().items() if k != "XDG_RUNTIME_DIR"}
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

    def test_autoplay_off_in_settings_file_keeps_new_message_silent(self) -> None:
        """--settings-file с autoplay: false — первое сообщение не играет само; play — играет."""
        settings = self.tmp / "prefs" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"tail": 5, "autoplay": False}))
        self.start_player("fakesink", None, "--settings-file", str(settings))
        self.drop(self.sentence(uuid.uuid4().hex, "aaaaaa", 0))
        self.assert_quiet()
        self.assertEqual(1, len(list(self.store.iterdir())))
        self.assertEqual(0, subprocess.run([str(BIN), "play"], env=self.env(),
                                           timeout=WAIT).returncode)
        self.assertTrue(self.played(1)[0].endswith("aaaaaa_s0.wav"))

    def test_settings_default_to_xdg_config_home(self) -> None:
        """Без --settings-file файл берётся из XDG_CONFIG_HOME подпроцесса."""
        settings = self.config / "tts-player" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"tail": 5, "autoplay": False}))
        self.start_player()
        self.drop(self.sentence(uuid.uuid4().hex, "aaaaaa", 0))
        self.assert_quiet()
        self.assertIsNone(self.proc.poll())

    def test_foreign_stream_ducked_while_playing_and_restored_after_end(self) -> None:
        """Первое сообщение — чужой поток до 30 %; склейка демона завершает сообщение,
        плеер уходит в idle — чужому потоку возвращается исходная громкость."""
        session = uuid.uuid4().hex
        self.start_player()
        sentence = self.sentence(session, "aaaaaa", 0)
        self.drop(sentence)
        self.played(1)
        self.assertEqual([["set-sink-input-volume", "7", "18000", "15000"]],
                         self.wait_volume_sets(1))
        self.daemon_saves(session, sentence)
        self.assertEqual(["set-sink-input-volume", "7", "60000", "50000"],
                         self.wait_volume_sets(2)[1])
        self.assertFalse((self.runtime / "tts-player" / "duck.json").exists())

    def daemon_saves(self, session: str, sentence: Path) -> None:
        """Склейка из одного предложения в speech_history и строка базы, как у демона."""
        created_at = time.time()
        name = f"tts-{created_at:.3f}-{session[:40]}.wav"
        (self.history / name).write_bytes(sentence.read_bytes())
        conn = sqlite3.connect(str(self.db))
        conn.execute(
            "INSERT INTO speech_history (file_name, created_at, session_id, project,"
            " persona, text, speed, tone, duration_seconds) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, created_at, session, "proj", "p", "t", 1.0, "neutral", SECONDS),
        )
        conn.commit()
        conn.close()

    def test_sigterm_exits_0(self) -> None:
        self.start_player()
        self.drop(self.sentence(uuid.uuid4().hex, "aaaaaa", 0))
        self.played(1)
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(0, self.proc.wait(timeout=WAIT))

    def test_sighup_exits_0_and_restores_volumes(self) -> None:
        self.start_player()
        self.drop(self.sentence(uuid.uuid4().hex, "aaaaaa", 0, seconds=3.0))
        self.wait_volume_sets(1)
        self.proc.send_signal(signal.SIGHUP)
        self.assertEqual(0, self.proc.wait(timeout=WAIT))
        self.assertEqual(["set-sink-input-volume", "7", "60000", "50000"], self.volume_sets()[-1])
        self.assertFalse((self.runtime / "tts-player" / "duck.json").exists())

    def test_leftover_duck_state_is_restored_at_start(self) -> None:
        state = self.runtime / "tts-player" / "duck.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"inputs": {"7": {
            "volumes": [60000, 50000], "name": "app", "pid": "4242"}}}))
        self.start_player()
        sets = self.wait_volume_sets(1)
        self.assertEqual(["set-sink-input-volume", "7", "60000", "50000"], sets[0])
        self.assertFalse(state.exists())


BIN = ROOT / "bin" / "tts-player"


class SingleInstanceTest(PlayerProcessCase):
    def run_bin(self, *args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess:
        return subprocess.run([str(BIN), *args], cwd=cwd, env=self.env(),
                              capture_output=True, text=True, timeout=WAIT)

    def test_second_start_exits_0_with_pid_and_keeps_store(self) -> None:
        self.start_player()
        self.drop(self.sentence(uuid.uuid4().hex, "aaaaaa", 0, seconds=3.0))
        self.played(1)
        kept = sorted(p.name for p in self.store.iterdir())
        self.assertTrue(kept)
        result = subprocess.run(
            self.command("--audio-sink", "fakesink", "--history-dir", str(self.history),
                         "--history-db", str(self.db)),
            cwd=ROOT, env=self.env(), capture_output=True, text=True, timeout=WAIT)
        self.assertEqual(0, result.returncode)
        self.assertIn(f"tts-player уже запущен (PID {self.proc.pid})", result.stderr)
        self.assertEqual(kept, sorted(p.name for p in self.store.iterdir()))

    def test_lock_is_free_after_exit_and_new_start_passes(self) -> None:
        self.start_player()
        self.drop(self.sentence(uuid.uuid4().hex, "aaaaaa", 0, seconds=3.0))
        self.played(1)
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(0, self.proc.wait(timeout=WAIT))
        self.start_player()
        self.drop(self.sentence(uuid.uuid4().hex, "bbbbbb", 0))
        names = self.played(2)  # недоигранное aaaaaa вернулось в inbox/ и играет снова
        self.assertTrue(names[0].endswith("aaaaaa_s0.wav") and names[1].endswith("bbbbbb_s0.wav"))

    def test_stop_without_player_exits_0_silently(self) -> None:
        result = self.run_bin("stop")
        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stderr)

    def test_stop_while_playing_logs_and_silences_until_next_play(self) -> None:
        session = uuid.uuid4().hex
        self.start_player()
        self.drop(self.sentence(session, "aaaaaa", 0, seconds=5.0))
        self.drop(self.sentence(session, "bbbbbb", 0))
        self.assertTrue(self.played(1)[0].endswith("aaaaaa_s0.wav"))
        result = self.run_bin("stop")
        self.assertEqual(0, result.returncode)
        self.next_line("stop requested")
        self.assert_quiet()
        self.assertIsNone(self.proc.poll())

    def test_play_without_player_exits_0_silently(self) -> None:
        result = self.run_bin("play")
        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stderr)

    def test_stop_then_play_resumes_and_play_while_playing_changes_nothing(self) -> None:
        session = uuid.uuid4().hex
        self.start_player()
        self.drop(self.sentence(session, "aaaaaa", 0, seconds=5.0))
        self.assertTrue(self.played(1)[0].endswith("aaaaaa_s0.wav"))
        self.assertEqual(0, self.run_bin("play").returncode)
        self.next_line("play requested")
        self.assert_quiet()
        self.assertEqual(0, self.run_bin("stop").returncode)
        self.next_line("stop requested")
        self.assertEqual(0, self.run_bin("play").returncode)
        self.next_line("play requested")
        self.assertTrue(self.played(1)[0].endswith("aaaaaa_s0.wav"))
        self.assertIsNone(self.proc.poll())

    def test_sigusr2_before_player_is_up_does_not_kill(self) -> None:
        self.start_player()
        lock = self.runtime / "tts-player" / "player.lock"
        deadline = time.monotonic() + WAIT
        while not (lock.exists() and lock.read_text().strip()):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.001)
        self.proc.send_signal(signal.SIGUSR2)
        self.drop(self.sentence(uuid.uuid4().hex, "aaaaaa", 0, seconds=5.0))
        self.played(1)
        self.assertIsNone(self.proc.poll())

    def test_bin_works_from_other_cwd_and_through_symlink(self) -> None:
        link = self.tmp / "tts-player-link"
        link.symlink_to(BIN)
        result = subprocess.run([str(link), "stop"], cwd=self.tmp, env=self.env(),
                                capture_output=True, text=True, timeout=WAIT)
        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stderr)

    def test_sigusr1_before_player_is_up_does_not_kill_and_stop_works_later(self) -> None:
        session = uuid.uuid4().hex
        self.start_player()
        lock = self.runtime / "tts-player" / "player.lock"
        deadline = time.monotonic() + WAIT
        while not (lock.exists() and lock.read_text().strip()):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.001)
        self.proc.send_signal(signal.SIGUSR1)
        self.drop(self.sentence(session, "aaaaaa", 0, seconds=5.0))
        self.assertTrue(self.played(1)[0].endswith("aaaaaa_s0.wav"))
        self.assertIsNone(self.proc.poll())
        self.assertEqual(0, self.run_bin("stop").returncode)
        self.next_line("stop requested")

    def test_stop_without_runtime_dir_exits_0(self) -> None:
        env = {k: v for k, v in self.env().items() if k != "XDG_RUNTIME_DIR"}
        result = subprocess.run([str(BIN), "stop"], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=WAIT)
        self.assertEqual(0, result.returncode)
        self.assertIn("плеер не запущен", result.stderr)

    def test_tray_conflicts(self) -> None:
        for extra in ("--show", "--no-gui"):
            result = subprocess.run(
                [sys.executable, "-m", "tts_player", "--tray", extra],
                cwd=ROOT, env=self.env(), capture_output=True, text=True, timeout=WAIT)
            self.assertEqual(2, result.returncode, extra)


if __name__ == "__main__":
    unittest.main()
