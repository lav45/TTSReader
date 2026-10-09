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
    в PATH (громкость настоящих потоков пользователя не меняется ни в одном e2e), свой HOME
    (обёртка paplay читает id задачи из временного .claude-tts/playback.json, а не из
    настоящего; без task() файла нет — id "unknown"; задачи демону плеер пишет во временную
    .claude-tts/queue, config.json там нет — кэш считается актуальным).

    drop() кладёт предложение, как демон: первое предложение нового token — сначала задача
    хука в hook-home (hook_task), id задачи демона — из файла, который плеер записал в
    .claude-tts/queue; его ставит task() перед обёрткой. Сессию задачи hook_task до первой
    её задачи регистрирует запросом add в control/ (register), как `tts-player-hook start`:
    задачи сессии не из списка плеер удаляет. Первая зарегистрированная сессия активна — в
    демон уходят только её задачи, поэтому тесты с задачами в демон держат одну сессию."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.runtime = self.tmp / "run"
        self.runtime.mkdir()
        self.config = self.tmp / "config"
        self.home = self.tmp / "home"
        self.home.mkdir()
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
        self.daemon_queue = self.home / ".claude-tts" / "queue"
        self.daemon_queue.mkdir(parents=True)
        self.hook_queue = self.runtime / "tts-player" / "hook-home" / ".claude-tts" / "queue"
        self.task_ids: dict[str, str] = {}  # token предложений → id задачи демона
        # открытая сессия прошлого запуска (sessions.json, контракт (15)): без окна сессию больше
        # нечем открыть, а задачи закрытой в демон не уходят
        self.session = uuid.uuid4().hex
        self.write_sessions(self.session, [self.session])
        self.registered: set[str] = {self.session}  # сессии, для которых положен запрос add
        self.hook_id = ""  # id последней задачи хука из hook_task()
        self.extra_env: dict[str, str] = {}  # поверх окружения процесса плеера

    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
        env["XDG_RUNTIME_DIR"] = str(self.runtime)
        env["XDG_CONFIG_HOME"] = str(self.config)
        env["HOME"] = str(self.home)
        env["PATH"] = f"{self.pactl_bin}{os.pathsep}{env.get('PATH', '')}"
        env["PACTL_LOG"] = str(self.pactl_log)
        env["PACTL_FIXTURE"] = str(self.pactl_fixture)
        for name in ("PACTL_SLEEP", "PACTL_EXIT"):
            env.pop(name, None)
        env.update(self.extra_env)
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

    def task(self, task_id: str) -> None:
        """Текущая задача демона, как её видит обёртка paplay: current_message.id.

        Демон пишет id только вместе с source задачи (claude_code_tts daemon.py:1499-1501).
        """
        claude_tts = self.home / ".claude-tts"
        claude_tts.mkdir(exist_ok=True)
        (claude_tts / "playback.json").write_text(json.dumps({"current_message": {
            "id": task_id, "source": "tts-player", "session_id": "s", "project": "p"}}))

    def write_sessions(self, active: str | None, ids: list[str]) -> None:
        path = self.runtime / "tts-player" / "sessions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"active": active,
                                    "sessions": [{"id": i, "project": "proj"} for i in ids]}))

    def register(self, session: str) -> None:
        """Запрос `session add` в control/, как его пишет CLI (.tmp, затем rename): плеер
        забирает его до задачи хука, запущен он или ещё нет."""
        control = self.runtime / "tts-player" / "control"
        control.mkdir(parents=True, exist_ok=True)
        name = f"{time.time_ns()}"
        tmp = control / f"{name}.tmp"
        tmp.write_text(json.dumps({"op": "add", "session": session, "cwd": "/w/proj"}))
        tmp.rename(control / f"{name}.json")
        self.registered.add(session)

    def hook_task(self, session: str, text: str) -> dict:
        """Задача хука в hook-home (как claude-tts: .tmp, затем rename); возвращает задачу,
        которую плеер записал в очередь демона. Сессия не зарегистрирована — сначала register.
        Стартующий плеер чистит queue/ (HookHome prepare) — задача, пропавшая без задачи
        демона, пишется заново."""
        if session not in self.registered:
            self.register(session)
        hook_id = self.hook_id = uuid.uuid4().hex[:16]
        task = {"id": hook_id, "timestamp": time.time(), "session_id": session,
                "project": "/w/proj", "text": text, "persona": "p", "speed": 1.0}
        target = self.hook_queue / f"{hook_id}.json"
        deadline = time.monotonic() + WAIT
        while True:
            self.assertLess(time.monotonic(), deadline, "задача не дошла до очереди демона")
            before = set(self.daemon_queue.glob("*.json"))
            try:
                tmp = self.hook_queue / f"{hook_id}.tmp"
                tmp.write_text(json.dumps(task))
                tmp.rename(target)
            except FileNotFoundError:  # каталога ещё нет или его почистили
                time.sleep(0.02)
                continue
            while target.exists():  # плеер удаляет задачу хука после записи задачи демона
                self.assertLess(time.monotonic(), deadline, "задача хука не забрана")
                time.sleep(0.02)
            new = set(self.daemon_queue.glob("*.json")) - before
            if new:
                [path] = new
                return json.loads(path.read_text())

    def drop(self, path: Path, key: str | None = None) -> None:
        """Предложение через обёртку paplay с id задачи демона его сообщения; key — token
        сообщения (по умолчанию из имени WAV), новый — сначала задача хука."""
        session, token = path.name.removeprefix("tts_queue_").rsplit("_", 2)[:2]
        key = token if key is None else key
        if key not in self.task_ids:
            self.task_ids[key] = self.hook_task(session, f"text {key}")["id"]
        self.task(self.task_ids[key])
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

    def wait_lock(self) -> None:
        """Плеер взял замок: старт прошёл, восстановление и inbox/ разобраны."""
        lock = self.runtime / "tts-player" / "player.lock"
        deadline = time.monotonic() + WAIT
        while not (lock.exists() and lock.read_text().strip()):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)

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
        session = self.session
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
        session = self.session
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

    def test_backlog_of_previous_run_is_deleted(self) -> None:
        """Файлы в inbox/ до старта несут id задач прошлого запуска — чужие (контракт (13))."""
        session = self.session
        self.task("0123456789abcdef")
        subprocess.run([str(PAPLAY), str(self.sentence(session, "aaaaaa", 0))], env=self.env(),
                       check=True)
        self.start_player()
        self.assertIn("0123456789abcdef", self.next_line("чужое предложение "))
        self.assert_quiet()
        self.assertEqual(([], []), (list(self.inbox.iterdir()), list(self.store.iterdir())))

    def test_unknown_task_sentence_is_deleted(self) -> None:
        session = self.session
        self.start_player()
        self.hook_task(session, "text")
        subprocess.run([str(PAPLAY), str(self.sentence(session, "aaaaaa", 0))], env=self.env(),
                       check=True)
        self.assertIn("_unknown_", self.next_line("чужое предложение "))
        self.assert_quiet()
        self.assertEqual([], list(self.store.iterdir()))

    def test_broken_wav_reports_error_and_next_still_plays(self) -> None:
        session = self.session
        self.start_player()
        self.drop(self.sentence(session, "aaaaaa", 0, valid=False))
        error = self.next_line("error ")
        self.assertTrue(error.startswith("error ") and "aaaaaa_s0.wav: " in error, error)
        self.drop(self.sentence(session, "bbbbbb", 0))
        self.assertTrue(self.next_line("playing ").endswith("bbbbbb_s0.wav"))

    def test_sigterm_keeps_unheard_and_restart_restores_them_waiting(self) -> None:
        """Выход сохраняет сообщения в messages.json, их файлы остаются в store/; следующий
        запуск их восстанавливает (файлы не удалены как ничьи), но сам ничего не играет."""
        session = self.session
        self.start_player()
        self.drop(self.sentence(session, "aaaaaa", 0, seconds=3.0))
        self.drop(self.sentence(session, "aaaaaa", 1))
        self.drop(self.sentence(session, "bbbbbb", 0))
        self.assertTrue(self.played(1)[0].endswith("aaaaaa_s0.wav"))
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(0, self.proc.wait(timeout=WAIT))
        self.assertEqual([], list(self.inbox.iterdir()))
        kept = sorted(p.name for p in self.store.iterdir())
        self.assertEqual(3, len(kept))
        self.assertTrue((self.runtime / "tts-player" / "messages.json").exists())
        self.start_player()
        self.wait_lock()
        self.assert_quiet()
        self.assertFalse((self.runtime / "tts-player" / "messages.json").exists())
        self.assertEqual(kept, sorted(p.name for p in self.store.iterdir()))

    def test_sentence_of_task_queued_before_exit_plays_after_restart(self) -> None:
        session = self.session
        self.start_player()
        self.task_ids["aaaaaa"] = self.hook_task(session, "text aaaaaa")["id"]
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(0, self.proc.wait(timeout=WAIT))
        self.start_player()
        self.drop(self.sentence(session, "aaaaaa", 0))
        self.assertTrue(self.next_line("playing ").endswith(
            f"_{self.task_ids['aaaaaa']}_tts_queue_{session}_aaaaaa_s0.wav"))

    def test_output_error_keeps_sentence_and_reports_once(self) -> None:
        session = self.session
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

    def assert_ducked_by_half(self) -> None:
        """Первое сообщение приглушает чужой поток (60000/50000) до duck = 50 % из файла."""
        self.drop(self.sentence(self.session, "aaaaaa", 0))
        self.played(1)
        self.assertEqual([["set-sink-input-volume", "7", "30000", "25000"]],
                         self.wait_volume_sets(1))

    def test_settings_file_option_is_read(self) -> None:
        settings = self.tmp / "prefs" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"tail": 5, "duck": 50}))
        self.start_player("fakesink", None, "--settings-file", str(settings))
        self.assert_ducked_by_half()

    def test_settings_default_to_xdg_config_home(self) -> None:
        """Без --settings-file файл берётся из XDG_CONFIG_HOME подпроцесса."""
        settings = self.config / "tts-player" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"tail": 5, "duck": 50}))
        self.start_player()
        self.assert_ducked_by_half()

    def test_foreign_stream_ducked_while_playing_and_restored_after_end(self) -> None:
        """Первое сообщение — чужой поток до 30 %; склейка демона завершает сообщение,
        плеер уходит в idle — чужому потоку возвращается исходная громкость."""
        session = self.session
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

    def test_orphan_stream_is_repaired_after_end(self) -> None:
        """Приглушённый чужой поток исчезает во время речи; после конца появляется новый
        поток того же приложения со сниженной громкостью — ему возвращается исходная."""
        session = self.session
        orphans = self.runtime / "tts-player" / "duck-orphans.json"
        self.start_player()
        sentence = self.sentence(session, "aaaaaa", 0)
        self.drop(sentence)
        self.played(1)
        self.wait_volume_sets(1)
        self.pactl_fixture.write_text("[]")
        deadline = time.monotonic() + WAIT
        while not orphans.exists():
            self.assertLess(time.monotonic(), deadline, "сирота не записана")
            time.sleep(0.05)
        self.daemon_saves(session, sentence)
        state = self.runtime / "tts-player" / "duck.json"
        deadline = time.monotonic() + WAIT
        while state.exists():
            self.assertLess(time.monotonic(), deadline, "громкость не возвращена")
            time.sleep(0.05)
        self.pactl_fixture.write_text(json.dumps([sink_input(9, 4242, [18000, 15000])]))
        sets = self.wait_volume_sets(2)
        self.assertEqual(["set-sink-input-volume", "9", "60000", "50000"], sets[1])
        deadline = time.monotonic() + WAIT
        while orphans.exists():
            self.assertLess(time.monotonic(), deadline, "сирота не снята")
            time.sleep(0.05)

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
        self.drop(self.sentence(self.session, "aaaaaa", 0))
        self.played(1)
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(0, self.proc.wait(timeout=WAIT))

    def test_sighup_exits_0_and_restores_volumes(self) -> None:
        self.start_player()
        self.drop(self.sentence(self.session, "aaaaaa", 0, seconds=3.0))
        self.wait_volume_sets(1)
        self.proc.send_signal(signal.SIGHUP)
        self.assertEqual(0, self.proc.wait(timeout=WAIT))
        self.assertEqual(["set-sink-input-volume", "7", "60000", "50000"], self.volume_sets()[-1])
        self.assertFalse((self.runtime / "tts-player" / "duck.json").exists())

    def test_leftover_duck_state_is_restored_at_start(self) -> None:
        state = self.runtime / "tts-player" / "duck.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"inputs": {"7": {
            "volumes": [60000, 50000], "name": "app4242", "pid": "4242"}}}))
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
        self.drop(self.sentence(self.session, "aaaaaa", 0, seconds=3.0))
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
        session = self.session
        self.start_player()
        self.drop(self.sentence(session, "aaaaaa", 0, seconds=3.0))
        self.played(1)
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(0, self.proc.wait(timeout=WAIT))
        self.start_player()
        self.drop(self.sentence(session, "bbbbbb", 0))
        # недоигранное aaaaaa сохранено при выходе, но само не играет: только новый ответ
        self.assertTrue(self.played(1)[0].endswith("bbbbbb_s0.wav"))

    def sessions_file(self) -> dict:
        return json.loads((self.runtime / "tts-player" / "sessions.json").read_text())

    def test_session_add_has_remove_cycle_through_player_process(self) -> None:
        self.start_player()
        self.wait_lock()
        added = self.run_bin("session", "add", "u1", "/p/TTSReader")
        self.assertEqual(0, added.returncode)
        self.assertEqual("сессия передана плееру\n", added.stdout)
        self.next_line("session add u1")
        self.assertEqual(0, self.run_bin("session", "has", "u1").returncode)
        self.assertEqual({"active": self.session,
                          "sessions": [{"id": self.session, "project": "proj"},
                                       {"id": "u1", "project": "TTSReader"}]},
                         self.sessions_file())  # новая сессия закрыта
        again = self.run_bin("session", "add", "u1", "/p/TTSReader")
        self.assertEqual(0, again.returncode)
        self.assertEqual("сессия уже в списке\n", again.stdout)
        removed = self.run_bin("session", "remove", "u1")
        self.assertEqual(0, removed.returncode)
        self.next_line("session remove u1")
        self.assertEqual(1, self.run_bin("session", "has", "u1").returncode)
        self.assertEqual({"active": self.session,
                          "sessions": [{"id": self.session, "project": "proj"}]},
                         self.sessions_file())

    def test_session_clear_hands_active_to_new_session_from_same_cwd(self) -> None:
        self.start_player()
        self.wait_lock()
        self.assertEqual(0, self.run_bin("session", "add", "b1", "/p/b").returncode)
        self.next_line("session add b1")
        self.assertEqual(self.session, self.sessions_file()["active"])
        self.assertEqual(0, self.run_bin("session", "clear", self.session, "/p/a").returncode)
        self.next_line(f"session clear {self.session}")
        self.assertIsNone(self.sessions_file()["active"])
        self.assertEqual(0, self.run_bin("session", "add", "c1", "/p/a").returncode)
        self.next_line("session replace c1")
        self.assertEqual("c1", self.sessions_file()["active"])

    def test_session_add_without_player_is_executed_at_player_start(self) -> None:
        added = self.run_bin("session", "add", "u2", "/p/X")
        self.assertEqual(0, added.returncode)
        self.assertIn("плеер не запущен", added.stdout)
        self.assertEqual(1, self.run_bin("session", "has", "u2").returncode)
        self.assertEqual(1, len(list((self.runtime / "tts-player" / "control").glob("*.json"))))
        self.start_player()
        self.next_line("session add u2")
        self.assertEqual(0, self.run_bin("session", "has", "u2").returncode)
        self.assertEqual([], list((self.runtime / "tts-player" / "control").glob("*.json")))

    def test_session_survives_player_restart(self) -> None:
        self.start_player()
        self.assertEqual(0, self.run_bin("session", "add", "u1", "/p/TTSReader").returncode)
        self.next_line("session add u1")
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(0, self.proc.wait(timeout=WAIT))
        self.assertEqual(1, self.run_bin("session", "has", "u1").returncode)  # плеера нет
        self.lines = queue.Queue()
        self.start_player()
        deadline = time.monotonic() + WAIT
        while self.run_bin("session", "has", "u1").returncode != 0:
            self.assertLess(time.monotonic(), deadline, "u1 не вернулась после перезапуска")
            time.sleep(0.1)
        self.assertIn("u1", [s["id"] for s in self.sessions_file()["sessions"]])

    def test_bin_works_from_other_cwd_and_through_symlink(self) -> None:
        link = self.tmp / "tts-player-link"
        link.symlink_to(BIN)
        result = subprocess.run([str(link), "session", "has", "u1"], cwd=self.tmp, env=self.env(),
                                capture_output=True, text=True, timeout=WAIT)
        self.assertEqual(1, result.returncode)  # плеера нет: сессии в списке нет
        self.assertEqual("", result.stderr)

    def test_stop_and_play_are_not_commands(self) -> None:
        for word in ("stop", "play"):
            result = self.run_bin(word)
            self.assertEqual(2, result.returncode, word)
            self.assertIn("unrecognized arguments", result.stderr)
        self.assertFalse((self.runtime / "tts-player" / "player.lock").exists())

    def test_hook_task_goes_to_daemon_queue_and_its_files_play(self) -> None:
        """Задача хука в hook-home → файл в очереди демона с source и новым id; предложения
        с этим id через настоящую обёртку (playback.json подставной) играют."""
        session = self.session
        self.start_player()
        task = self.hook_task(session, "Привет")
        self.assertEqual(("tts-player", "Привет", session, "/w/proj", "p", 1.0),
                         (task["source"], task["text"], task["session_id"], task["project"],
                          task["persona"], task["speed"]))
        self.assertRegex(task["id"], "^[0-9a-f]{16}$")
        self.assertEqual(f"task {self.hook_id} {session} 6 chars", self.next_line("task "))
        self.assertNotEqual(self.hook_id, task["id"])
        self.task(task["id"])
        subprocess.run([str(PAPLAY), str(self.sentence(session, "aaaaaa", 0))], env=self.env(),
                       check=True)
        self.assertTrue(self.next_line("playing ").endswith(
            f"_{task['id']}_tts_queue_{session}_aaaaaa_s0.wav"))

    def test_one_sentence_answer_ends_by_count_and_restores_volume(self) -> None:
        """Счётчик предложений в окружении «claude-tts» (поддельный claude_code_tts под текущим
        интерпретатором): ответ из одного предложения кончается сразу после него — без склейки
        и следующей задачи громкость чужого потока возвращается."""
        fake = self.tmp / "fake" / "claude_code_tts"
        fake.mkdir(parents=True)
        (fake / "__init__.py").write_text("")
        (fake / "bridge.py").write_text("def split_sentences(text):\n    return [text.strip()]\n")
        self.extra_env["PYTHONPATH"] = str(fake.parent)
        session = self.session
        self.start_player("fakesink", None, "--tts-python", sys.executable)
        task = self.hook_task(session, "Привет")
        self.task(task["id"])
        subprocess.run([str(PAPLAY), str(self.sentence(session, "aaaaaa", 0))], env=self.env(),
                       check=True)
        self.played(1)
        self.assertEqual(["set-sink-input-volume", "7", "60000", "50000"],
                         self.wait_volume_sets(2)[1])

    def test_hook_home_links_follow_tts_config_dir(self) -> None:
        """--tts-config-dir задаёт и ссылки hook-home: хук читает тот же config.json, что и
        плеер."""
        tts_dir = self.tmp / "tts-config"
        tts_dir.mkdir()
        self.start_player("fakesink", None, "--tts-config-dir", str(tts_dir))
        link = self.runtime / "tts-player" / "hook-home" / ".claude-tts" / "config.json"
        deadline = time.monotonic() + WAIT
        while not link.is_symlink():
            self.assertLess(time.monotonic(), deadline, "нет ссылки config.json в hook-home")
            time.sleep(0.02)
        self.assertEqual(tts_dir / "config.json", Path(os.readlink(link)))

    def test_hook_task_is_taken_and_logged(self) -> None:
        claude_tts = self.home / ".claude-tts"
        (claude_tts / "daemon.heartbeat").write_text(str(time.time()))
        self.start_player()
        hook_base = self.runtime / "tts-player" / "hook-home" / ".claude-tts"
        queue_dir = hook_base / "queue"
        deadline = time.monotonic() + WAIT
        while not queue_dir.is_dir():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.register("sess")
        tmp = queue_dir / "1.tmp"
        tmp.write_text(json.dumps({"id": "abc", "session_id": "sess", "text": "Привет"}))
        tmp.rename(queue_dir / "1.json")
        self.assertEqual("task abc sess 6 chars", self.next_line("task "))
        while (queue_dir / "1.json").exists():  # удаляется после записи задачи демона
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.assertFalse((self.runtime / "hook-home").exists())
        while not (hook_base / "daemon.heartbeat").exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)

    def add_session(self, session: str) -> None:
        """`session add` через настоящий CLI; ждёт строку плеера, что сессия в списке."""
        self.assertEqual(0, self.run_bin("session", "add", session, "/w/proj").returncode)
        self.next_line(f"session add {session}")

    def put_hook_task(self, session: str) -> str:
        """Задача хука в hook-home без ожидания задачи демона (отложенную и удалённую она не
        рождает); возвращает id задачи хука."""
        hook_id = uuid.uuid4().hex[:16]
        task = {"id": hook_id, "timestamp": time.time(), "session_id": session,
                "project": "/w/proj", "text": f"text {hook_id}", "persona": "p", "speed": 1.0}
        deadline = time.monotonic() + WAIT
        while not self.hook_queue.is_dir():
            self.assertLess(time.monotonic(), deadline, "нет очереди hook-home")
            time.sleep(0.02)
        tmp = self.hook_queue / f"{hook_id}.tmp"
        tmp.write_text(json.dumps(task))
        tmp.rename(self.hook_queue / f"{hook_id}.json")
        return hook_id

    def daemon_tasks(self) -> set[Path]:
        return set(self.daemon_queue.glob("*.json"))

    def test_deferred_task_of_inactive_session_does_not_reach_daemon(self) -> None:
        self.start_player()
        self.wait_lock()
        self.add_session("sessb")
        self.assertEqual(self.session, self.sessions_file()["active"])
        before = self.daemon_tasks()
        hook_id = self.put_hook_task("sessb")
        self.assertEqual(f"task {hook_id} sessb deferred", self.next_line(f"task {hook_id} sessb d"))
        self.assertEqual(before, self.daemon_tasks())
        task = self.hook_task(self.session, "Привет")
        self.assertEqual(self.session, task["session_id"])
        self.assertEqual(1, len(self.daemon_tasks() - before))

    def test_task_of_session_not_in_list_is_dropped(self) -> None:
        self.start_player()
        self.wait_lock()
        self.add_session("sessa")
        before = self.daemon_tasks()
        hook_id = self.put_hook_task("ghost")
        self.assertEqual(f"task {hook_id} ghost dropped: not in list",
                         self.next_line(f"task {hook_id} ghost"))
        self.assertEqual(before, self.daemon_tasks())
        self.assertFalse((self.hook_queue / f"{hook_id}.json").exists())

    def test_restart_keeps_messages_per_session_and_deferred_is_not_sent(self) -> None:
        self.start_player()
        self.wait_lock()
        self.add_session("sessb")
        self.drop(self.sentence(self.session, "aaaaaa", 0, seconds=3.0))
        self.drop(self.sentence(self.session, "aaaaaa", 1))
        self.drop(self.sentence(self.session, "cccccc", 0))
        self.played(1)
        hook_id = self.put_hook_task("sessb")
        self.next_line(f"task {hook_id} sessb deferred")
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(0, self.proc.wait(timeout=WAIT))
        self.assertEqual([(self.session, False), (self.session, False), ("sessb", True)],
                         self.saved_messages())
        sent = self.daemon_tasks()
        self.lines = queue.Queue()
        self.start_player()
        self.wait_lock()
        self.assert_quiet()  # при старте сам ничего не играет: ни открытая, ни sessb
        self.assertEqual(sent, self.daemon_tasks())
        self.proc.send_signal(signal.SIGTERM)  # отложенное sessb восстановлено — снова в снимке
        self.assertEqual(0, self.proc.wait(timeout=WAIT))
        self.assertEqual([(self.session, False), (self.session, False), ("sessb", True)],
                         self.saved_messages())

    def saved_messages(self) -> list[tuple[str, bool]]:
        """(сессия, deferred) сообщений messages.json по порядку."""
        saved = json.loads((self.runtime / "tts-player" / "messages.json").read_text())
        return [(m["session_id"], m["deferred"]) for m in saved["messages"]]

    def test_session_remove_deletes_its_message_files(self) -> None:
        self.start_player()
        self.wait_lock()
        self.add_session("sessa")
        self.drop(self.sentence(self.session, "bbbbbb", 0, seconds=3.0))
        self.drop(self.sentence(self.session, "bbbbbb", 1))
        self.played(1)
        self.assertTrue(any(f"_{self.session}_" in p.name for p in self.store.iterdir()))
        self.assertEqual(0, self.run_bin("session", "remove", self.session).returncode)
        self.next_line(f"session remove {self.session}")
        deadline = time.monotonic() + WAIT
        while any(f"_{self.session}_" in p.name for p in self.store.iterdir()):
            self.assertLess(time.monotonic(), deadline, "файлы сессии остались в store/")
            time.sleep(0.05)
        self.assertEqual({"active": None, "sessions": [{"id": "sessa", "project": "proj"}]},
                         self.sessions_file())  # убранная открытая — открытой не остаётся

    def test_tray_conflicts(self) -> None:
        for extra in ("--show", "--no-gui"):
            result = subprocess.run(
                [sys.executable, "-m", "tts_player", "--tray", extra],
                cwd=ROOT, env=self.env(), capture_output=True, text=True, timeout=WAIT)
            self.assertEqual(2, result.returncode, extra)


if __name__ == "__main__":
    unittest.main()
