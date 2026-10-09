"""Тесты Player: задачи хука в демон и переозвучка (заглушки DaemonQueue и DaemonSettings),
воспроизведение по курсору, кнопки, снимок состояния, выход процесса, повтор при отказе вывода
звука, переход играющего сообщения на склейку демона, приглушение чужих звуков на время
playing (заглушка Ducker)."""
import json
import tempfile
import time
import unittest
import wave
from pathlib import Path
from typing import Callable
from unittest import mock

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from tts_player.daemon_settings import ActiveVoice  # noqa: E402
from tts_player.history import HistoryEntry  # noqa: E402
from tts_player.inbox import NAME_PATTERN  # noqa: E402
from tts_player.model import HookTask, Message, Sentence  # noqa: E402
from tts_player.settings_error import SettingsError  # noqa: E402
from tts_player.player import Player, PlayerState  # noqa: E402
from tts_player.player_settings import Prefs  # noqa: E402
from tts_player.sessions import Session, Sessions  # noqa: E402

# <recv_ns>_<id задачи демона>_<имя WAV демона>; id задачи = token, кроме N0
A0 = "1000000000000000001_aa_tts_queue_S_aa_s0.wav"
A1 = "1000000000000000002_aa_tts_queue_S_aa_s1.wav"
B0 = "1000000000000000003_bb_tts_queue_S_bb_s0.wav"
N0 = "2000000000000000004_dd_tts_queue_S_dd_s0.wav"  # пришло после склейки: склейка S его не берёт
C0 = "1000000000000000005_cc_tts_queue_S_cc_s0.wav"
JOINED = "tts-2000000000.000-S.wav"
RATE = 1000
SECONDS = 0.5


def write_wav(path: Path, seconds: float) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"\0\0" * int(seconds * RATE))


class FakePlayback:
    """Подменяет Playback: запоминает play(), колбэки зовёт тест."""

    def __init__(self, audio_sink: str | None = None) -> None:
        self.on_finished: Callable[[Path], None] = lambda path: None
        self.on_error: Callable[[Path, str, bool], None] = lambda path, text, output_failed: None
        self.on_started: Callable[[Path], None] = lambda path: None
        self.played: list[str] = []
        self.starts: list[tuple[str, float]] = []
        self.at: float | None = None  # что вернёт position()
        self.playing: Path | None = None
        self.stops = 0

    def play(self, path: Path, start_s: float = 0.0) -> None:
        self.played.append(path.name)
        self.starts.append((path.name, start_s))
        self.playing = path

    def position(self) -> float | None:
        return self.at if self.playing is not None else None

    def stop(self) -> None:
        self.playing = None
        self.stops += 1

    def started(self) -> None:
        """Конвейер дошёл до PLAYING: звук пошёл."""
        self.on_started(self.playing)

    def finish(self) -> None:
        """EOS играющего файла: как настоящий Playback — сначала stop, потом колбэк."""
        path, self.playing = self.playing, None
        self.on_finished(path)

    def fail(self, text: str, output_failed: bool) -> None:
        """Ошибка играющего файла: как настоящий Playback — сначала stop, потом колбэк."""
        path, self.playing = self.playing, None
        self.on_error(path, text, output_failed)


class FakeDucker:
    """Подменяет Ducker: запоминает вызовы, pactl не зовёт никогда."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.has_orphans = False
        self.orphans_changed_at = 0.0

    def watch(self) -> None:
        self.calls.append(("watch",))

    def duck(self, percent: int) -> None:
        self.calls.append(("duck", percent))

    def refresh(self) -> None:
        self.calls.append(("refresh",))

    def restore(self) -> None:
        self.calls.append(("restore",))


class FakeDaemonQueue:
    """Подменяет DaemonQueue: запоминает поля задач; id — по очереди из ids, кончились — None
    (сбой записи)."""

    def __init__(self) -> None:
        self.ids: list[str] = []
        self.puts: list[dict] = []

    def put(self, fields: dict) -> str | None:
        self.puts.append(fields)
        return self.ids.pop(0) if self.ids else None


class FakeSettings:
    """Подменяет DaemonSettings: текущая персона p со скоростью 1.0; error — сбой config.json."""

    def __init__(self) -> None:
        self.persona = "p"
        self.speed = 1.0
        self.error: SettingsError | None = None
        self.reads = 0

    def active_voice(self) -> ActiveVoice:
        self.reads += 1
        if self.error is not None:
            raise self.error
        return ActiveVoice(self.persona, self.speed, {
            "persona": self.persona, "speed": self.speed, "speed_method": "tempo",
            "voice_sherpa": f"voice-{self.persona}"})


class FakeCounter:
    """Подменяет SentenceCounter: запоминает запросы, отвечает тест (answer); не ответил —
    как недоступный счётчик."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, Callable[[int], None]]] = []
        self.stopped = False

    def count(self, text: str, on_count: Callable[[int], None]) -> None:
        self.requests.append((text, on_count))

    def answer(self, count: int, request: int = 0) -> None:
        self.requests[request][1](count)

    def stop(self) -> None:
        self.stopped = True


class PlayerCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runtime = Path(tmp.name)
        self.inbox_dir = self.runtime / "tts-player" / "inbox"
        self.store_dir = self.runtime / "tts-player" / "store"
        self.inbox_dir.mkdir(parents=True)
        history_dir = self.runtime / "speech_history"
        history_dir.mkdir()
        self.history_dir = history_dir
        patcher = mock.patch("tts_player.player.Playback", FakePlayback)
        patcher.start()
        self.addCleanup(patcher.stop)
        glib = mock.patch("tts_player.player.GLib")
        self.glib = glib.start()
        self.addCleanup(glib.stop)
        self.glib.timeout_add_seconds.return_value = 42
        self.daemon = FakeDaemonQueue()
        self.settings = FakeSettings()
        self.player = Player(self.runtime, None, history_dir, self.runtime / "handy_analysis.db",
                             sessions=Sessions(self.runtime / "sessions.json"),
                             daemon_queue=self.daemon, daemon_settings=self.settings)
        self.player.add_session("S", "/w/proj")  # сессия задач хука тестов, открыта
        self.player.select_session("S")
        self.playback: FakePlayback = self.player._playback
        self.states: list[PlayerState] = []
        self.player.on_change = self.states.append

    def announce(self, task_id: str, session: str = "S", player: Player | None = None) -> None:
        """Задача хука пришла и ушла в демон с id task_id (текущая персона p, скорость 1.0)."""
        player = self.player if player is None else player
        self.daemon.ids.append(task_id)
        player._on_hook_task(HookTask(f"hook-{task_id}", 1.0, session, "proj", f"text {task_id}",
                                      "p", 1.0, {"id": f"hook-{task_id}",
                                                 "text": f"text {task_id}"}))

    def announce_names(self, names: tuple[str, ...]) -> None:
        """Задачи хука для предложений names, каждая — до первого своего предложения."""
        for name in names:
            match = NAME_PATTERN.match(name)
            if not any(match["task_id"] in m.task_ids for m in self.player._queue.messages):
                self.announce(match["task_id"], match["session_id"])

    def start_with(self, *names: str) -> None:
        """Задачи хука, затем их предложения в inbox/ до старта: настоящие WAV по SECONDS."""
        self.announce_names(names)
        for name in names:
            write_wav(self.inbox_dir / name, SECONDS)
        self.player.start()

    def arrive(self, name: str, seconds: float = SECONDS) -> None:
        """Предложение, забранное inbox/ после старта; задача хука для него — заранее."""
        self.announce_names((name,))
        path = self.store_dir / name
        write_wav(path, seconds)
        match = NAME_PATTERN.match(name)
        self.player._on_sentence(Sentence(int(match["recv_ns"]), match["task_id"],
                                          match["session_id"], match["token"],
                                          int(match["index"]), path))

    def path(self, name: str) -> Path:
        return self.store_dir / name

    def finish(self) -> None:
        """Играющий файл доиграл."""
        self.playback.finish()

    def retry(self) -> None:
        """Срабатывание таймера повтора, поставленного последним."""
        seconds, callback, *data = self.glib.timeout_add_seconds.call_args.args
        self.assertEqual(2, seconds)
        callback(*data)

    def joined(self, seconds: float = 1.0) -> HistoryEntry:
        """Склейка A0+A1 сессии S, как её пишет демон."""
        path = self.history_dir / JOINED
        write_wav(path, seconds)
        return HistoryEntry(path, 2_000_000_000.0, "S", "proj", seconds)

    def state(self) -> PlayerState:
        return self.player.state()

    def saved_file(self) -> Path:
        return self.runtime / "tts-player" / "messages.json"

    def restart(self) -> None:
        """Новый процесс плеера в том же $XDG_RUNTIME_DIR (после shutdown прежнего); сессии
        прошлого запуска — из sessions.json, как в __main__."""
        sessions = Sessions(self.runtime / "sessions.json")
        sessions.load()
        self.player = Player(self.runtime, None, self.history_dir,
                             self.runtime / "handy_analysis.db", sessions=sessions,
                             daemon_queue=self.daemon, daemon_settings=self.settings)
        self.playback = self.player._playback
        self.player.on_change = self.states.append
        self.player.start()

    def play_first(self, tail: int | None = None) -> None:
        """Клик по первому сообщению и play/pause: сам плеер после старта ничего не начинает,
        а play/pause без текущего играет последнее.
        tail — после клика: с фокусом очередь не режется, по нему — только хвост начатых."""
        self.player.play_message(self.state().messages[0])
        if tail is not None:
            self.player.apply_prefs(Prefs(tail=tail))
        self.player.toggle_play_pause()

    def task(self) -> str | None:
        """id задачи демона текущего сообщения (последней, если их несколько)."""
        current = self.state().current
        return None if current is None else self.task_of(current)

    def task_of(self, message: Message) -> str | None:
        return self.player._queue.task_of(message)


class PlayerHistoryTest(PlayerCase):
    """Склейка, отказ вывода, выход процесса — прежнее поведение."""

    def switch_on_a1(self) -> None:
        """Играет A1 (0,2 с от начала), приходит склейка A — переход на неё."""
        self.start_with(A0, A1, N0)
        self.finish()
        self.playback.at = 0.2
        self.player._on_entry(self.joined())

    def test_entry_switches_playing_message_at_its_position(self) -> None:
        with self.assertLogs("tts_player", "INFO") as logs:
            self.switch_on_a1()
        self.assertEqual([(A0, 0.0), (A1, 0.0), (JOINED, 0.7)], self.playback.starts)
        self.assertEqual(["complete S aa 1.00s", f"switched {JOINED} at 0.70"],
                         [r.getMessage() for r in logs.records
                          if r.getMessage().startswith(("complete", "switched"))])
        self.finish()
        self.assertEqual([A0, A1, JOINED, N0], self.playback.played)

    def test_state_lengths_match_queue_length_per_message(self) -> None:
        self.start_with(A0, A1, N0)
        self.player._on_entry(self.joined())
        state = self.state()
        self.assertEqual(len(state.messages), len(state.lengths))
        self.assertEqual([self.player._queue.length(m) for m in state.messages], state.lengths)
        self.assertAlmostEqual(1.0, state.lengths[0])

    def test_entry_during_outage_waits_for_retry_of_joined_file(self) -> None:
        self.start_with(A0, A1, N0)
        self.finish()
        self.playback.fail("no output", True)
        self.player._on_entry(self.joined())
        self.assertEqual([A0, A1], self.playback.played)
        self.retry()
        self.assertEqual((JOINED, 0.5), self.playback.starts[-1])
        self.finish()
        self.assertEqual([A0, A1, JOINED, N0], self.playback.played)

    def test_entry_while_paused_is_remembered_and_resume_plays_joined(self) -> None:
        self.start_with(A0, A1, N0)
        self.finish()
        self.playback.at = 0.2
        self.player.toggle_play_pause()
        with self.assertLogs("tts_player", "INFO") as logs:
            self.player._on_entry(self.joined())
        self.assertEqual([], [r.getMessage() for r in logs.records
                              if r.getMessage().startswith("switched")])
        self.assertEqual("paused", self.state().status)
        self.player.toggle_play_pause()
        self.assertEqual(JOINED, self.playback.starts[-1][0])
        self.assertAlmostEqual(0.7, self.playback.starts[-1][1])

    def test_entry_of_message_waiting_at_its_end_moves_on(self) -> None:
        self.start_with(A0, A1)
        self.finish()
        self.finish()
        self.assertEqual("playing", self.state().status)
        self.player._on_entry(self.joined())
        self.assertEqual("idle", self.state().status)
        self.assertEqual("aa", self.task())
        self.assertTrue(self.state().current.heard)

    def test_shutdown_during_joined_file_keeps_unfinished_sentences(self) -> None:
        """Выход на 0,7 с склейки: непрослушанное — в messages.json; доигранное A0 и склейка
        удалены, недоигранное A1 и следующее N0 — в store/."""
        self.switch_on_a1()
        self.playback.at = 0.7  # склейка играет с 0,7 с
        self.player.shutdown()
        self.assertEqual([], list(self.inbox_dir.iterdir()))
        self.assertEqual(sorted([A1, N0]),
                         sorted(p.name for p in self.store_dir.iterdir()))
        self.assertTrue(self.saved_file().exists())

    def test_store_is_taken_from_inbox(self) -> None:
        self.assertEqual(self.store_dir, self.player._inbox.store)

    def test_shutdown_saves_messages_and_restart_waits_then_plays_unheard(self) -> None:
        """Перезапуск: сообщения восстановлены, сам плеер ничего не играет (idle, текущего
        нет); щелчок по первому и play — непрослушанные играют по порядку."""
        self.start_with(A0, A1, B0)
        self.assertEqual([A0], self.playback.played)
        self.playback.at = 0.3
        self.player.shutdown()
        self.assertEqual([], list(self.inbox_dir.iterdir()))
        self.assertEqual(sorted([A0, A1, B0]), sorted(p.name for p in self.store_dir.iterdir()))
        self.restart()
        self.assertFalse(self.saved_file().exists())
        self.assertEqual(["aa", "bb"], [self.task_of(m) for m in self.state().messages])
        self.assertEqual(["text aa", "text bb"], [m.text for m in self.state().messages])
        self.assertEqual(("idle", None, []),
                         (self.state().status, self.state().current, self.playback.starts))
        self.play_first()
        self.assertEqual([(A0, 0.0)], self.playback.starts)
        self.finish()
        self.finish()
        self.assertEqual([A0, A1, B0], self.playback.played)
        self.assertEqual(1, len(self.daemon.puts) - 1)  # переозвучки нет: кэш актуален

    def test_sentence_in_inbox_of_task_sent_before_exit_plays_after_restart(self) -> None:
        """Задача ушла в демон до выхода, её предложение легло в inbox/ без плеера: после
        старта это новый ответ — играет само."""
        self.player.start()
        self.announce("aa")
        self.player.shutdown()
        write_wav(self.inbox_dir / A0, SECONDS)
        self.restart()
        self.assertEqual(("playing", "aa"), (self.state().status, self.task()))
        self.assertEqual([A0], self.playback.played)

    def test_restart_resumes_current_from_unfinished_sentence(self) -> None:
        """Как прежний возврат store/ → inbox/: доигранные до позиции предложения текущего
        удаляются при выходе, следующий запуск играет с недоигранного."""
        self.start_with(A0, A1, B0)
        self.finish()
        self.playback.at = 0.2  # играет A1: позиция 0,7 в сообщении aa
        self.player.shutdown()
        self.assertEqual(sorted([A1, B0]), sorted(p.name for p in self.store_dir.iterdir()))
        self.restart()
        self.assertEqual([], self.playback.starts)
        self.play_first()
        self.assertEqual([(A1, 0.0)], self.playback.starts)
        self.finish()
        self.assertEqual(B0, self.playback.played[-1])

    def test_new_message_after_clear_and_restart_plays_all_sentences(self) -> None:
        """Очистка, выход, старт: новые задачи идут после доставленных до очистки — новое
        сообщение растёт, а не завершено с первого предложения."""
        self.start_with(A0, B0)
        self.arrive(C0)
        self.player.clear()
        self.player.shutdown()
        self.restart()
        self.arrive("1000000000000000006_ee_tts_queue_S_ee_s0.wav")
        message = self.state().current
        self.assertFalse(self.player._queue.is_complete(message))
        self.finish()
        self.assertFalse(message.heard)
        self.arrive("1000000000000000007_ee_tts_queue_S_ee_s1.wav")
        self.assertEqual("1000000000000000007_ee_tts_queue_S_ee_s1.wav", self.playback.played[-1])
        self.assertEqual(4, len(self.daemon.puts))  # aa, bb, cc, ee — без повторной задачи

    def test_heard_message_is_saved_without_files(self) -> None:
        self.start_with(A0, B0)
        self.finish()
        self.player.shutdown()
        self.assertEqual([B0], [p.name for p in self.store_dir.iterdir()])
        self.restart()
        heard, unheard = self.state().messages
        self.assertEqual((True, [], "text aa"), (heard.heard, heard.sentences, heard.text))
        self.assertEqual([self.path(B0)], [s.path for s in unheard.sentences])

    def test_sentence_of_task_queued_before_exit_plays_after_restart(self) -> None:
        self.player.start()
        self.announce("aa")
        self.player.shutdown()
        self.restart()
        self.arrive(A0)
        self.assertEqual([A0], self.playback.played)

    def test_store_leftovers_not_in_saved_messages_are_deleted_at_start(self) -> None:
        self.store_dir.mkdir(parents=True)
        write_wav(self.store_dir / B0, SECONDS)
        with self.assertLogs("tts_player.inbox", "WARNING"):
            self.player.start()
        self.assertEqual([], list(self.store_dir.iterdir()))

    def test_file_error_skips_to_next(self) -> None:
        self.start_with(A0, B0)
        with self.assertLogs("tts_player", "INFO") as logs:
            self.playback.fail("broken", False)
        self.assertEqual([A0, B0], self.playback.played)
        self.assertIn(f"error {A0}: broken", logs.output[0])
        self.glib.timeout_add_seconds.assert_not_called()

    def test_broken_first_sentence_is_tried_not_skipped(self) -> None:
        self.announce("aa")
        (self.inbox_dir / A0).write_bytes(b"not a wav")
        write_wav(self.inbox_dir / A1, SECONDS)
        with self.assertLogs("tts_player.queue", "WARNING"):  # длительность не-WAV не читается
            self.player.start()
        self.assertEqual([(A0, 0.0)], self.playback.starts)
        self.playback.fail("broken", False)
        self.assertEqual([(A0, 0.0), (A1, 0.0)], self.playback.starts)

    def test_output_error_retries_same_sentence_and_reports_once(self) -> None:
        self.start_with(A0, B0)
        with self.assertLogs("tts_player", "INFO") as logs:
            self.playback.fail("no output", True)
            self.assertTrue(self.state().output_down)
            self.assertEqual([A0], self.playback.played)
            self.retry()
            self.assertEqual([A0, A0], self.playback.played)
            self.playback.fail("no output", True)
            self.retry()
            self.assertEqual([A0, A0, A0], self.playback.played)
            self.finish()
        self.assertEqual([A0, A0, A0, B0], self.playback.played)
        self.assertFalse(self.state().output_down)
        errors = [line for line in logs.output if "error " in line]
        self.assertEqual(1, len(errors), logs.output)
        self.assertIn(f"error {A0}: no output", errors[0])
        playing = [line for line in logs.output if "playing " in line]
        self.assertEqual(1, len(playing), logs.output)  # повтор строки playing не пишет

    def test_new_outage_is_reported_again(self) -> None:
        self.start_with(A0, B0)
        with self.assertLogs("tts_player", "INFO") as logs:
            self.playback.fail("no output", True)
            self.retry()
            self.finish()
            self.playback.fail("no output", True)
        errors = [line for line in logs.output if "error " in line]
        self.assertEqual(2, len(errors), logs.output)

    def test_new_sentence_during_outage_does_not_jump_in(self) -> None:
        self.start_with(A0)
        self.playback.fail("no output", True)
        self.announce("bb")
        write_wav(self.inbox_dir / B0, SECONDS)
        deadline = time.monotonic() + 5
        while not self.path(B0).exists() and time.monotonic() < deadline:
            GLib.MainContext.default().iteration(False)
        self.assertTrue(self.path(B0).exists())
        self.assertEqual([A0], self.playback.played)

    def test_shutdown_cancels_pending_retry(self) -> None:
        self.start_with(A0)
        self.playback.fail("no output", True)
        self.player.shutdown()
        self.glib.source_remove.assert_called_once_with(42)
        self.assertEqual([], list(self.inbox_dir.iterdir()))

    def test_successful_retry_clears_outage_and_position_moves(self) -> None:
        self.start_with(A0, A1)
        self.playback.fail("no output", True)
        self.retry()
        self.playback.at = 0.3
        self.assertTrue(self.state().output_down)  # позиция — ещё не звук
        count = len(self.states)
        self.playback.started()
        self.assertEqual(count + 1, len(self.states))
        state = self.states[-1]
        self.assertFalse(state.output_down)
        self.assertAlmostEqual(0.3, state.position_s)
        self.player.toggle_play_pause()
        self.assertAlmostEqual(0.3, self.state().position_s)
        self.player.toggle_play_pause()
        self.assertEqual(A0, self.playback.starts[-1][0])
        self.assertAlmostEqual(0.3, self.playback.starts[-1][1])

    def test_outage_after_recovery_is_reported_again_without_state_calls(self) -> None:
        self.player.on_change = lambda state: None  # --no-gui: state() никто не зовёт
        self.start_with(A0, A1)
        with self.assertLogs("tts_player", "INFO") as logs:
            self.playback.fail("no output", True)
            self.retry()
            self.playback.started()
            self.playback.at = 0.3
            self.playback.fail("no output", True)
        errors = [line for line in logs.output if "error " in line]
        self.assertEqual(2, len(errors), logs.output)

    def test_retry_from_offset_is_not_progress(self) -> None:
        self.start_with(A0, A1)
        self.player.seek(0.2)
        self.playback.fail("no output", True)
        self.retry()
        self.playback.at = 0.2  # Playback до seek отдаёт позицию старта
        self.assertTrue(self.state().output_down)
        with self.assertNoLogs("tts_player", "INFO"):  # тот же отказ — без новой строки error
            self.playback.fail("no output", True)
        self.assertTrue(self.state().output_down)

    def test_next_during_outage_plays_and_recovers(self) -> None:
        self.start_with(A0, B0)
        self.playback.fail("no output", True)
        self.player.next()
        self.glib.source_remove.assert_called_once_with(42)
        self.assertEqual((B0, 0.0), self.playback.starts[-1])
        self.playback.started()
        self.playback.at = 0.1
        state = self.state()
        self.assertEqual((False, "bb"), (state.output_down, self.task_of(state.current)))
        self.assertAlmostEqual(0.1, state.position_s)

    def test_pause_during_outage_cancels_retry_and_resume_plays_same_place(self) -> None:
        self.start_with(A0, A1)
        self.finish()
        self.playback.fail("no output", True)
        self.player.toggle_play_pause()
        self.glib.source_remove.assert_called_once_with(42)
        state = self.state()
        self.assertEqual("paused", state.status)
        self.assertAlmostEqual(0.5, state.position_s)
        self.player.toggle_play_pause()
        self.assertEqual((A1, 0.0), self.playback.starts[-1])

    def test_seek_during_outage_plays_target(self) -> None:
        self.start_with(A0, A1)
        self.playback.fail("no output", True)
        self.player.seek(0.7)
        self.glib.source_remove.assert_called_once_with(42)
        name, start = self.playback.starts[-1]
        self.assertEqual(A1, name)
        self.assertAlmostEqual(0.2, start)
        self.playback.started()
        self.playback.at = 0.4
        state = self.state()
        self.assertFalse(state.output_down)
        self.assertAlmostEqual(0.9, state.position_s)

    def test_change_callback_exception_is_logged_and_state_stays_consistent(self) -> None:
        self.start_with(A0, B0)

        def broken(state: PlayerState) -> None:
            raise RuntimeError("window broke")

        self.player.on_change = broken
        with self.assertLogs("tts_player.player", "ERROR") as logs:
            self.player.next()
        self.assertIn("window broke", logs.output[0] + "".join(
            str(r.exc_info[1]) for r in logs.records if r.exc_info))
        self.assertEqual(("playing", "bb"), (self.state().status, self.task()))
        self.assertEqual((B0, 0.0), self.playback.starts[-1])

    def test_without_history_plays(self) -> None:
        player = Player(self.runtime, None, None, None,
                        sessions=Sessions(self.runtime / "sessions.json"),
                        daemon_queue=self.daemon, daemon_settings=self.settings)
        player.add_session("S", "/w/proj")
        player.select_session("S")
        self.announce("aa", player=player)
        write_wav(self.inbox_dir / A0, SECONDS)
        player.start()
        self.assertEqual([A0], player._playback.played)
        player.shutdown()


class PlayerCursorTest(PlayerCase):
    """Автопереход и снимок состояния."""

    def test_idle_without_messages(self) -> None:
        self.player.start()
        state = self.state()
        self.assertEqual("idle", state.status)
        self.assertIsNone(state.current)
        self.assertEqual((0.0, 0.0, [], False),
                         (state.position_s, state.length_s, state.messages, state.output_down))

    def test_state_while_playing(self) -> None:
        self.start_with(A0, A1, B0)
        self.finish()
        self.playback.at = 0.2
        state = self.state()
        self.assertEqual(("playing", "aa"), (state.status, self.task_of(state.current)))
        self.assertAlmostEqual(0.7, state.position_s)
        self.assertAlmostEqual(1.0, state.length_s)
        self.assertEqual(["aa", "bb"], [self.task_of(m) for m in state.messages])

    def test_sentences_of_message_play_in_order_then_next_message(self) -> None:
        self.start_with(A0, A1, B0)
        self.finish()
        self.finish()
        self.assertEqual([A0, A1, B0], self.playback.played)
        self.assertTrue(self.state().messages[0].heard)

    def test_last_message_waits_for_more_sentences(self) -> None:
        self.start_with(A0)
        self.finish()
        state = self.state()
        self.assertEqual(("playing", "aa"), (state.status, self.task_of(state.current)))
        self.assertAlmostEqual(0.5, state.position_s)
        self.arrive(A1)
        self.assertEqual([(A0, 0.0), (A1, 0.0)], self.playback.starts)
        self.assertAlmostEqual(0.5, self.state().position_s)

    def test_new_token_finishes_waiting_message(self) -> None:
        self.start_with(A0)
        self.finish()
        self.arrive(B0)
        self.assertEqual([A0, B0], self.playback.played)
        self.assertTrue(self.state().messages[0].heard)

    def test_idle_keeps_finished_current_and_new_message_plays(self) -> None:
        self.start_with(A0)
        self.finish()
        self.player._on_entry(self.joined(seconds=3.0))  # другая длина: конец без склейки
        state = self.state()
        self.assertEqual(("idle", "aa"), (state.status, self.task_of(state.current)))
        self.arrive(B0)
        self.assertEqual(("playing", "bb"), (self.state().status, self.task()))
        self.assertEqual([A0, B0], self.playback.played)

    def unknown(self, name: str) -> None:
        """Предложение без id задачи (обёртка не нашла current_message), без задачи хука."""
        path = self.store_dir / name
        write_wav(path, SECONDS)
        match = NAME_PATTERN.match(name)
        self.player._on_sentence(Sentence(int(match["recv_ns"]), match["task_id"] or "unknown",
                                          match["session_id"], match["token"],
                                          int(match["index"]), path))

    def test_unknown_sentence_of_known_pass_joins_its_message(self) -> None:
        """playback.json потерял current_message посреди прохода: остальные предложения пришли
        с id unknown, но метка прохода (aa) та же — они идут в сообщение этого прохода."""
        self.start_with(A0)
        self.play_first()
        later = "1000000000000000002_unknown_tts_queue_S_aa_s1.wav"
        with self.assertLogs("tts_player.player", "INFO") as logs:
            self.unknown(later)
        self.assertIn("предложение без id задачи отнесено к задаче aa по проходу aa: " + later,
                      [r.getMessage() for r in logs.records])
        self.finish()
        self.assertEqual((later, 0.0), self.playback.starts[-1])
        self.assertEqual([A0, later], [s.path.name for s in self.state().current.sentences])

    def test_unknown_sentence_of_unknown_pass_is_still_foreign(self) -> None:
        self.start_with(A0)
        self.play_first()
        stray = "1000000000000000002_unknown_tts_queue_S_bc_s1.wav"
        self.unknown(stray)
        self.assertFalse(self.path(stray).exists())
        self.assertEqual([A0], [s.path.name for s in self.state().current.sentences])
    def test_later_pass_duplicate_replaces_unplayed_sentence(self) -> None:
        """Пауза демона: проход оборван на s1, новый проход (token ab) начат с s1 — пока играет
        s0, его s1 заменяет прежнее, повтора нет."""
        self.start_with(A0, A1)
        self.play_first()
        later = "1000000000000000009_aa_tts_queue_S_ab_s1.wav"
        self.arrive(later)
        self.finish()
        self.assertEqual((later, 0.0), self.playback.starts[-1])
        self.assertFalse(self.path(A1).exists())

    def test_later_pass_duplicate_of_playing_sentence_is_dropped(self) -> None:
        self.start_with(A0, A1)
        self.play_first()
        self.finish()  # играет A1
        later = "1000000000000000009_aa_tts_queue_S_ab_s1.wav"
        self.arrive(later)
        self.assertEqual((A1, 0.0), self.playback.starts[-1])
        self.assertEqual([A0, A1], [s.path.name for s in self.state().current.sentences])

    def test_tail_keeps_five_started_messages(self) -> None:
        """Сообщения приходят, пока играет первое (фокус): копятся; next по ним вытесняет
        самое старое начатое сверх хвоста."""
        names = [f"100000000000000000{i}_{t}_tts_queue_S_{t}_s0.wav"
                 for i, t in enumerate(["a1", "a2", "a3", "a4", "a5", "a6"], start=1)]
        self.start_with(names[0])
        for name in names[1:]:
            self.arrive(name)
        self.assertEqual(6, len(self.state().messages))
        for _ in range(5):
            self.player.next()
        self.assertEqual(["a2", "a3", "a4", "a5", "a6"], [self.task_of(m) for m in self.state().messages])
        self.assertFalse(self.path(names[0]).exists())

    def test_on_change_after_file_change_and_new_sentence(self) -> None:
        self.start_with(A0, A1)
        self.announce("bb")
        count = len(self.states)
        self.assertGreater(count, 0)
        self.finish()
        self.assertEqual(count + 1, len(self.states))
        self.arrive(B0)
        self.assertEqual(count + 2, len(self.states))
        self.assertEqual(["aa", "bb"], [self.task_of(m) for m in self.states[-1].messages])


class PlayerButtonsTest(PlayerCase):
    def test_play_pause_keeps_position_and_resumes_from_it(self) -> None:
        self.start_with(A0, A1, B0)
        self.finish()
        self.playback.at = 0.2
        self.player.toggle_play_pause()
        self.assertIsNone(self.playback.playing)
        self.playback.at = 0.4
        state = self.state()
        self.assertEqual("paused", state.status)
        self.assertAlmostEqual(0.7, state.position_s)
        self.player.toggle_play_pause()
        self.assertEqual("playing", self.state().status)
        self.assertEqual(A1, self.playback.starts[-1][0])
        self.assertAlmostEqual(0.2, self.playback.starts[-1][1])

    def test_paused_player_does_not_move_on(self) -> None:
        self.start_with(A0)
        self.finish()
        self.player.toggle_play_pause()
        self.arrive(B0)
        self.assertEqual(("paused", "aa"), (self.state().status, self.task()))
        self.assertEqual([A0], self.playback.played)

    def test_next_plays_following_from_start_and_marks_left_heard(self) -> None:
        self.start_with(A0, A1, B0)
        self.playback.at = 0.3
        self.player.next()
        state = self.state()
        self.assertEqual(("playing", "bb", 0.0), (state.status, self.task_of(state.current),
                                                  self.player._position))
        self.assertEqual((B0, 0.0), self.playback.starts[-1])
        self.assertTrue(state.messages[0].heard)

    def test_prev_from_incomplete_message_keeps_it_unheard_and_sentences_arrive(self) -> None:
        self.start_with(A0, B0)
        self.player.next()
        self.assertEqual("bb", self.task())
        self.player.prev()
        bb = self.state().messages[1]
        self.assertFalse(bb.heard)
        self.assertTrue(self.state().messages[0].heard)
        self.assertTrue(self.path(B0).exists())
        self.arrive("1000000000000000006_bb_tts_queue_S_bb_s1.wav")
        bb = self.state().messages[1]
        self.assertEqual(2, len(bb.sentences))
        self.assertFalse(bb.heard)
        self.assertTrue(all(s.path.exists() for s in bb.sentences))

    def click_second_message(self, before: str) -> None:
        """Клик по второму сообщению из статуса before: только выбор, звука нет, в хвост не входит."""
        self.assertEqual(before, self.state().status)
        starts = list(self.playback.starts)
        self.player.play_message(self.player._queue.messages[1])
        bb = self.state().messages[1]
        self.assertEqual(("stopped", "bb", 0.0), (self.state().status, self.task(),
                                                  self.player._position))
        self.assertEqual(starts, self.playback.starts)
        self.assertFalse(self.player._queue.is_started(bb))
        self.player.toggle_play_pause()
        self.assertEqual(("playing", "bb"), (self.state().status, self.task()))
        self.assertEqual((B0, 0.0), self.playback.starts[-1])
        self.assertTrue(self.player._queue.is_started(bb))

    def test_play_message_while_playing_only_selects_it(self) -> None:
        self.start_with(A0, A1, B0)
        self.click_second_message("playing")

    def test_play_message_while_paused_only_selects_it(self) -> None:
        self.start_with(A0, A1, B0)
        self.player.toggle_play_pause()
        self.click_second_message("paused")

    def test_play_message_heard_only_selects_it(self) -> None:
        self.start_with(A0, B0)
        self.player.next()
        self.assertTrue(self.state().messages[0].heard)
        starts = list(self.playback.starts)
        self.playback.at = 0.3
        self.player.play_message(self.player._queue.messages[0])
        self.assertEqual(("stopped", "aa", 0.0), (self.state().status, self.task(),
                                                  self.player._position))
        self.assertEqual(starts, self.playback.starts)
        # выбор не снимает отметку «прослушано»
        self.assertTrue(self.state().messages[0].heard)

    def test_play_message_marks_left_complete_heard_and_growing_not(self) -> None:
        self.start_with(A0, A1, B0)
        self.player.play_message(self.player._queue.messages[1])
        self.assertTrue(self.state().messages[0].heard)
        self.player.play_message(self.player._queue.messages[0])
        self.assertFalse(self.state().messages[1].heard)

    def assert_click_on_current_changes_nothing(self) -> None:
        current = self.player._queue.messages[0]
        before = (self.state().status, self.player._position, list(self.playback.starts),
                  self.state().messages[0].heard, len(self.states))
        self.player.play_message(current)
        after = (self.state().status, self.player._position, list(self.playback.starts),
                 self.state().messages[0].heard, len(self.states))
        self.assertEqual(before, after)
        self.assertFalse(self.state().messages[0].heard)

    def test_play_message_current_changes_nothing(self) -> None:
        # A завершено (за ним есть B): _leave(current) пометила бы его прослушанным
        self.start_with(A0, B0)
        self.playback.at = 0.3
        self.assert_click_on_current_changes_nothing()

    def test_play_message_current_while_paused_changes_nothing(self) -> None:
        self.start_with(A0, B0)
        self.player.toggle_play_pause()
        self.assert_click_on_current_changes_nothing()

    def test_play_message_current_during_outage_keeps_retry(self) -> None:
        self.start_with(A0, B0)
        self.playback.fail("no output", True)
        self.player.play_message(self.player._queue.messages[0])
        self.glib.source_remove.assert_not_called()

    def test_play_message_not_in_queue_changes_nothing(self) -> None:
        self.start_with(A0, B0)
        stale = self.player._queue.messages[1]
        self.player.clear()
        self.arrive(C0)
        starts = list(self.playback.starts)
        token = self.task()
        self.player.play_message(stale)
        self.assertEqual(starts, self.playback.starts)
        self.assertEqual(token, self.task())

    def test_play_message_current_while_idle_changes_nothing(self) -> None:
        self.start_with(A0)
        self.finish()
        self.player._on_entry(self.joined(seconds=3.0))  # другая длина: конец без склейки
        self.assertEqual(("idle", "aa"), (self.state().status, self.task()))
        starts = list(self.playback.starts)
        self.player.play_message(self.player._queue.messages[0])
        self.assertEqual(("idle", "aa"), (self.state().status, self.task()))
        self.assertEqual(starts, self.playback.starts)

    def test_play_message_other_during_outage_cancels_retry(self) -> None:
        self.start_with(A0, B0)
        self.playback.fail("no output", True)
        self.assertTrue(self.state().output_down)
        self.player.play_message(self.player._queue.messages[1])
        self.glib.source_remove.assert_called_once_with(42)
        self.assertEqual(("stopped", "bb", False),
                         (self.state().status, self.task(), self.state().output_down))

    def test_next_on_last_does_nothing(self) -> None:
        self.start_with(A0, B0)
        self.player.next()
        played = list(self.playback.played)
        self.player.next()
        self.assertEqual(played, self.playback.played)
        self.assertEqual("bb", self.task())

    def test_prev_plays_preceding_from_start(self) -> None:
        self.start_with(A0, B0, C0)
        self.player.next()
        self.playback.at = 0.3
        self.player.prev()
        state = self.state()
        self.assertEqual(("playing", "aa"), (state.status, self.task_of(state.current)))
        self.assertEqual((A0, 0.0), self.playback.starts[-1])
        self.assertTrue(state.messages[1].heard)

    def test_prev_on_first_does_nothing(self) -> None:
        self.start_with(A0, B0)
        self.player.prev()
        self.assertEqual([A0], self.playback.played)
        self.assertEqual("aa", self.task())

    def test_prev_while_paused_plays(self) -> None:
        self.start_with(A0, B0)
        self.player.next()
        self.player.toggle_play_pause()
        self.player.prev()
        self.assertEqual(("playing", "aa"), (self.state().status, self.task()))

    def test_seek_before_joined_plays_sentence_at_offset(self) -> None:
        self.start_with(A0, A1, B0)
        self.player.seek(0.7)
        name, start = self.playback.starts[-1]
        self.assertEqual(A1, name)
        self.assertAlmostEqual(0.2, start)
        self.playback.at = start
        self.assertAlmostEqual(0.7, self.state().position_s)
        self.finish()
        self.assertEqual(B0, self.playback.played[-1])

    def test_seek_on_pause_stays_paused_and_resume_starts_there(self) -> None:
        self.start_with(A0, A1, B0)
        self.player.toggle_play_pause()
        played = list(self.playback.played)
        self.player.seek(0.7)
        state = self.state()
        self.assertEqual("paused", state.status)
        self.assertAlmostEqual(0.7, state.position_s)
        self.assertEqual(played, self.playback.played)
        self.player.toggle_play_pause()
        name, start = self.playback.starts[-1]
        self.assertEqual(A1, name)
        self.assertAlmostEqual(0.2, start)

    def test_seek_clamps_to_message(self) -> None:
        self.start_with(A0, A1)
        self.player.toggle_play_pause()
        self.player.seek(99)
        self.assertAlmostEqual(1.0, self.state().position_s)
        self.player.seek(-1)
        self.assertEqual(0.0, self.state().position_s)

    def test_seek_after_joined_plays_joined_at_position(self) -> None:
        self.start_with(A0, A1, N0)
        self.player._on_entry(self.joined())
        self.player.seek(0.3)
        self.assertEqual(JOINED, self.playback.starts[-1][0])
        self.assertAlmostEqual(0.3, self.playback.starts[-1][1])

    def test_clear_empties_store_and_next_sentence_plays(self) -> None:
        self.start_with(A0, A1, B0)
        self.player.clear()
        state = self.state()
        self.assertEqual(("idle", None, []), (state.status, state.current, state.messages))
        self.assertIsNone(self.playback.playing)
        self.assertEqual([], list(self.store_dir.iterdir()))
        self.arrive(C0)
        self.assertEqual(("playing", "cc"), (self.state().status, self.task()))
        self.assertEqual(C0, self.playback.played[-1])

    def test_on_change_after_each_command(self) -> None:
        self.start_with(A0, A1, B0)
        commands = [self.player.toggle_play_pause, self.player.toggle_play_pause,
                    self.player.next, self.player.prev,
                    lambda: self.player.seek(0.2), self.player.clear]
        for command in commands:
            count = len(self.states)
            command()
            self.assertEqual(count + 1, len(self.states), command)
            self.assertEqual(self.state(), self.states[-1])


class PlayerPrefsTest(PlayerCase):
    """Настройки плеера (контракт (6)): хвост; команда play/pause."""

    def tasks(self) -> list[str]:
        return [self.task_of(m) for m in self.state().messages]

    def manual(self) -> None:
        """Плеер сам ничего не начинает — так он ведёт себя, пока звучит свой текст (hold);
        задачи хука при этом уходят в демон, как без удержания."""
        self.player._starts_by_itself = lambda: False

    def test_tail_is_5_by_default(self) -> None:
        self.assertEqual(5, self.player._queue.tail)

    def test_apply_prefs_sets_tail_and_notifies(self) -> None:
        self.player.start()
        count = len(self.states)
        self.player.apply_prefs(Prefs(tail=2))
        self.assertEqual(2, self.player._queue.tail)
        self.assertEqual(count + 1, len(self.states))

    def test_play_pause_when_playing_pauses_at_position(self) -> None:
        self.start_with(A0, B0)
        self.playback.at = 0.2
        self.player.toggle_play_pause()
        state = self.state()
        self.assertEqual("paused", state.status)
        self.assertAlmostEqual(0.2, state.position_s)
        self.assertIsNone(self.playback.playing)

    def test_play_pause_when_paused_resumes_from_position(self) -> None:
        self.start_with(A0, B0)
        self.playback.at = 0.2
        self.player.toggle_play_pause()
        self.player.toggle_play_pause()
        self.assertEqual("playing", self.state().status)
        name, start = self.playback.starts[-1]
        self.assertEqual(A0, name)
        self.assertAlmostEqual(0.2, start)

    def test_play_pause_when_stopped_plays_current_from_cursor(self) -> None:
        self.end_last_manually()
        self.assertEqual("stopped", self.state().status)
        self.player.seek(0.3)
        self.player.toggle_play_pause()
        self.assertEqual(("playing", "aa"), (self.state().status, self.task()))
        name, start = self.playback.starts[-1]
        self.assertEqual(JOINED, name)
        self.assertAlmostEqual(0.3, start)

    def test_play_pause_when_idle_at_end_plays_current_from_start(self) -> None:
        self.start_with(A0, A1)
        self.finish()
        self.finish()
        self.player._on_entry(self.joined())
        self.assertEqual(("idle", "aa"), (self.state().status, self.task()))
        self.player.toggle_play_pause()
        self.assertEqual("playing", self.state().status)
        self.assertEqual((JOINED, 0.0), self.playback.starts[-1])

    def test_play_pause_without_current_plays_last_message(self) -> None:
        self.manual()
        self.start_with(A0, B0)
        self.player.toggle_play_pause()
        self.assertEqual(("playing", "bb"), (self.state().status, self.task()))
        self.assertEqual([(B0, 0.0)], self.playback.starts)

    def test_play_pause_without_current_plays_last_even_if_heard(self) -> None:
        """Последнее прослушано, раньше есть непрослушанное: играет всё равно последнее."""
        self.manual()
        self.start_with(A0, B0)
        self.player._queue.mark_heard(self.state().messages[-1])
        self.player.toggle_play_pause()
        self.assertEqual(("playing", "bb"), (self.state().status, self.task()))
        self.assertEqual([(B0, 0.0)], self.playback.starts)

    def test_play_pause_without_messages_does_nothing(self) -> None:
        self.player.start()
        self.player.toggle_play_pause()
        self.assertEqual(("idle", None), (self.state().status, self.state().current))
        self.assertEqual([], self.playback.starts)

    def test_manual_end_of_message_stops_on_following_at_start(self) -> None:
        self.manual()
        self.start_with(A0, B0)
        self.play_first()
        self.finish()
        state = self.state()
        self.assertEqual(("stopped", "bb", 0.0), (state.status, self.task(), state.position_s))
        self.assertTrue(state.messages[0].heard)
        self.assertEqual([A0], self.playback.played)
        self.assertIsNone(self.playback.playing)
        self.player.toggle_play_pause()
        self.assertEqual((B0, 0.0), self.playback.starts[-1])

    def test_manual_end_of_last_message_keeps_it_at_its_end(self) -> None:
        self.manual()
        self.start_with(A0, A1)
        self.player.toggle_play_pause()
        self.finish()
        self.finish()
        self.player._on_entry(self.joined())
        state = self.state()
        self.assertEqual(("stopped", "aa"), (state.status, self.task()))
        self.assertAlmostEqual(state.length_s, state.position_s)
        self.assertTrue(state.current.heard)
        self.assertEqual([A0, A1], self.playback.played)

    def end_last_manually(self) -> None:
        """Без автовоспроизведения доиграно последнее сообщение aa: stopped в его конце."""
        self.manual()
        self.start_with(A0, A1)
        self.player.toggle_play_pause()
        self.finish()
        self.finish()
        self.player._on_entry(self.joined())

    def test_play_pause_after_last_ended_plays_message_arrived_since(self) -> None:
        self.end_last_manually()
        self.arrive(B0)
        self.assertEqual(("stopped", "aa"), (self.state().status, self.task()))
        self.player.toggle_play_pause()
        state = self.state()
        self.assertEqual(("playing", "bb", 0.0), (state.status, self.task(), state.position_s))
        self.assertEqual((B0, 0.0), self.playback.starts[-1])

    def test_play_pause_after_last_ended_without_new_replays_it(self) -> None:
        self.end_last_manually()
        self.player.toggle_play_pause()
        self.assertEqual(("playing", "aa"), (self.state().status, self.task()))
        self.assertEqual((JOINED, 0.0), self.playback.starts[-1])

    def test_play_pause_in_idle_after_end_plays_message_arrived_since(self) -> None:
        """Сам перестал начинать после конца (idle): новое не играет само, play/pause — играет."""
        self.start_with(A0, A1)
        self.finish()
        self.finish()
        self.player._on_entry(self.joined())
        self.manual()
        self.arrive(B0)
        self.assertEqual(("idle", "aa"), (self.state().status, self.task()))
        self.player.toggle_play_pause()
        self.assertEqual(("playing", "bb"), (self.state().status, self.task()))
        self.assertEqual((B0, 0.0), self.playback.starts[-1])

    def test_click_in_idle_without_current_selects_without_playing(self) -> None:
        self.manual()
        self.start_with()
        self.arrive(A0)
        self.arrive(B0)
        self.assertEqual("idle", self.state().status)
        self.assertIsNone(self.state().current)
        self.player.play_message(self.player._queue.messages[1])
        bb = self.state().messages[1]
        self.assertEqual(("stopped", "bb", 0.0), (self.state().status, self.task(),
                                                  self.player._position))
        self.assertEqual([], self.playback.starts)
        self.assertFalse(self.player._queue.is_started(bb))
        self.player.toggle_play_pause()
        self.assertEqual((B0, 0.0), self.playback.starts[-1])
        self.assertTrue(self.player._queue.is_started(bb))

    def test_click_in_idle_with_finished_current_selects_other(self) -> None:
        self.start_with(A0, A1)
        self.finish()
        self.finish()
        self.player._on_entry(self.joined())
        self.manual()
        self.arrive(B0)
        self.assertEqual(("idle", "aa"), (self.state().status, self.task()))
        starts = list(self.playback.starts)
        self.player.play_message(self.player._queue.messages[1])
        bb = self.state().messages[1]
        self.assertEqual(("stopped", "bb", 0.0), (self.state().status, self.task(),
                                                  self.player._position))
        self.assertEqual(starts, self.playback.starts)
        self.assertFalse(self.player._queue.is_started(bb))
        # покинутое aa доиграно и начато: прослушано
        self.assertTrue(self.state().messages[0].heard)
        self.player.play_message(self.player._queue.messages[0])
        self.assertEqual(("stopped", "aa"), (self.state().status, self.task()))
        self.assertEqual(starts, self.playback.starts)

    def stop_on_bb_manually(self, tail: int = 5) -> None:
        """Без автовоспроизведения доиграно aa: текущее — bb, не начатое (stopped на 0)."""
        self.manual()
        self.start_with(A0, B0, C0)
        self.play_first(tail)
        self.finish()
        self.assertEqual(("stopped", "bb"), (self.state().status, self.task()))

    def heard(self) -> dict[str, bool]:
        return {self.task_of(m): m.heard for m in self.state().messages}

    def test_next_from_unplayed_current_keeps_it_unheard_and_out_of_tail(self) -> None:
        self.stop_on_bb_manually(tail=1)
        bb = self.state().current
        self.player.next()
        self.assertEqual(("playing", "cc"), (self.state().status, self.task()))
        self.assertEqual({"bb": False, "cc": False}, self.heard())
        self.assertFalse(self.player._queue.is_started(bb))

    def test_prev_from_unplayed_current_keeps_it_unheard(self) -> None:
        self.stop_on_bb_manually()
        bb = self.state().current
        self.player.prev()
        self.assertEqual(("playing", "aa"), (self.state().status, self.task()))
        self.assertFalse(bb.heard)
        self.assertFalse(self.player._queue.is_started(bb))

    def test_click_from_unplayed_current_keeps_it_unheard(self) -> None:
        self.stop_on_bb_manually()
        bb = self.state().current
        self.player.play_message(self.state().messages[2])
        self.assertEqual(("stopped", "cc"), (self.state().status, self.task()))
        self.assertFalse(bb.heard)
        self.assertFalse(self.player._queue.is_started(bb))

    def test_play_pause_after_joined_longer_than_sentences_plays_new(self) -> None:
        """Склейка длиннее предложений (в пределах допуска): курсор доигранного — в её конце."""
        self.manual()
        self.start_with(A0, A1)
        self.player.toggle_play_pause()
        self.finish()
        self.finish()
        self.player._on_entry(self.joined(1.04))
        state = self.state()
        self.assertEqual("stopped", state.status)
        self.assertAlmostEqual(1.04, state.position_s)
        self.arrive(B0)
        self.player.toggle_play_pause()
        self.assertEqual(("playing", "bb"), (self.state().status, self.task()))
        self.assertEqual((B0, 0.0), self.playback.starts[-1])

    def test_manual_new_message_in_idle_only_appears_in_list(self) -> None:
        self.manual()
        self.player.start()
        self.arrive(A0)
        state = self.state()
        self.assertEqual(("idle", None), (state.status, state.current))
        self.assertEqual(["aa"], self.tasks())
        self.assertEqual([], self.playback.played)

    def test_manual_play_of_following_enters_tail(self) -> None:
        self.manual()
        self.start_with(A0, B0)
        self.play_first(tail=1)
        self.finish()
        self.assertEqual(["aa", "bb"], self.tasks())
        self.player.toggle_play_pause()
        self.assertEqual(["bb"], self.tasks())

    def test_play_pause_in_silence_plays_last_message(self) -> None:
        self.manual()
        self.start_with(A0, B0, C0)
        self.player.toggle_play_pause()
        self.assertEqual(("playing", "cc"), (self.state().status, self.task()))
        self.assertEqual([(C0, 0.0)], self.playback.starts)

    def test_on_change_after_play_pause(self) -> None:
        self.start_with(A0, B0)
        for _ in range(3):
            count = len(self.states)
            self.player.toggle_play_pause()
            self.assertEqual(count + 1, len(self.states))
            self.assertEqual(self.state(), self.states[-1])



class PlayerDuckingTest(PlayerCase):
    """Приглушение (контракт (9)): вход в playing — duck(prefs.duck), выход — restore,
    пока playing — refresh по таймеру раз в 1 с."""

    def setUp(self) -> None:
        super().setUp()
        self.ducker = FakeDucker()
        self.now = 0.0
        self.player = Player(self.runtime, None, self.history_dir,
                             self.runtime / "handy_analysis.db", ducker=self.ducker,
                             clock=lambda: self.now,
                             sessions=Sessions(self.runtime / "sessions.json"),
                             daemon_queue=self.daemon, daemon_settings=self.settings)
        self.player.add_session("S", "/w/proj")
        self.player.select_session("S")
        self.playback = self.player._playback
        self.player.on_change = self.states.append

    def calls(self) -> list[tuple]:
        return [c for c in self.ducker.calls if c != ("refresh",)]

    def refresh_timers(self) -> list:
        """Колбэки таймеров refresh (раз в 1 с), в порядке постановки."""
        return [c.args[1] for c in self.glib.timeout_add_seconds.call_args_list
                if c.args[0] == 1]

    def test_start_of_playback_ducks_once_with_default_percent(self) -> None:
        self.start_with(A0, A1, B0)
        self.finish()
        self.finish()
        self.assertEqual([("duck", 30)], self.calls())

    def test_pause_restores_and_resume_ducks(self) -> None:
        self.start_with(A0, B0)
        self.player.toggle_play_pause()
        self.assertEqual([("duck", 30), ("restore",)], self.calls())
        self.player.toggle_play_pause()
        self.assertEqual([("duck", 30), ("restore",), ("duck", 30)], self.calls())

    def test_end_without_following_restores(self) -> None:
        self.start_with(A0)
        self.finish()
        self.player._on_entry(self.joined(seconds=3.0))
        self.assertEqual("idle", self.state().status)
        self.assertEqual([("duck", 30), ("restore",)], self.calls())

    def test_waiting_for_next_sentence_keeps_ducked(self) -> None:
        self.start_with(A0)
        self.finish()
        self.assertEqual("playing", self.state().status)
        self.arrive(A1)
        self.finish()
        self.assertEqual([("duck", 30)], self.calls())

    def test_next_while_playing_keeps_ducked(self) -> None:
        self.start_with(A0, B0)
        self.player.next()
        self.assertEqual([("duck", 30)], self.calls())

    def test_clear_restores(self) -> None:
        self.start_with(A0, B0)
        self.player.clear()
        self.assertEqual([("duck", 30), ("restore",)], self.calls())

    def test_shutdown_while_playing_restores(self) -> None:
        self.start_with(A0)
        self.player.shutdown()
        self.assertEqual([("duck", 30), ("restore",)], self.calls())
        self.glib.source_remove.assert_called_with(42)

    def test_shutdown_when_idle_does_not_restore_again(self) -> None:
        self.player.start()
        self.player.shutdown()
        self.assertEqual([], self.calls())

    def test_refresh_timer_only_while_playing(self) -> None:
        self.player.start()
        self.assertEqual([], self.refresh_timers())
        self.arrive(A0)
        self.arrive(B0)
        timers = self.refresh_timers()
        self.assertEqual(1, len(timers))
        timers[0]()
        self.assertEqual(("refresh",), self.ducker.calls[-1])
        self.glib.source_remove.assert_not_called()
        self.player.toggle_play_pause()
        self.glib.source_remove.assert_called_once_with(42)
        self.player.toggle_play_pause()
        self.assertEqual(2, len(self.refresh_timers()))

    def watch_timers(self) -> list:
        """Колбэки таймеров watch (раз в 2 с), в порядке постановки."""
        return [c.args[1] for c in self.glib.timeout_add_seconds.call_args_list
                if c.args[0] == 2]

    def test_watch_timer_while_orphans_and_not_playing(self) -> None:
        self.start_with(A0, B0)
        self.ducker.has_orphans = True
        self.assertEqual([], self.watch_timers())
        self.player.toggle_play_pause()
        timers = self.watch_timers()
        self.assertEqual(1, len(timers))
        self.assertEqual(self.glib.SOURCE_CONTINUE, timers[0]())
        self.assertEqual(("watch",), self.ducker.calls[-1])
        self.player.toggle_play_pause()
        self.assertEqual(1, len(self.watch_timers()))
        self.glib.source_remove.assert_any_call(42)

    def test_watch_timer_removed_when_orphans_gone(self) -> None:
        self.ducker.has_orphans = True
        self.player.start()
        timers = self.watch_timers()
        self.assertEqual(1, len(timers))
        self.ducker.has_orphans = False
        self.assertEqual(self.glib.SOURCE_REMOVE, timers[0]())

    def test_watch_stops_ten_minutes_after_last_orphan_change(self) -> None:
        self.ducker.has_orphans = True
        self.player.start()
        timer = self.watch_timers()[0]
        self.now = 599.0
        self.assertEqual(self.glib.SOURCE_CONTINUE, timer())
        self.now = 600.0
        self.assertEqual(self.glib.SOURCE_REMOVE, timer())
        self.player.apply_prefs(Prefs(duck=30))
        self.assertEqual(1, len(self.watch_timers()))

    def test_watch_not_started_when_orphans_are_old(self) -> None:
        self.ducker.has_orphans = True
        self.now = 700.0
        self.player.start()
        self.assertEqual([], self.watch_timers())

    def test_watch_restarts_when_orphans_change(self) -> None:
        self.ducker.has_orphans = True
        self.now = 700.0
        self.player.start()
        self.ducker.orphans_changed_at = 690.0
        self.player.apply_prefs(Prefs(duck=30))
        self.assertEqual(1, len(self.watch_timers()))

    def test_no_watch_timer_without_orphans(self) -> None:
        self.player.start()
        self.assertEqual([], self.watch_timers())

    def test_shutdown_removes_watch_timer(self) -> None:
        self.ducker.has_orphans = True
        self.player.start()
        self.player.shutdown()
        self.glib.source_remove.assert_called_with(42)

    def test_apply_prefs_new_duck_while_playing_reducks(self) -> None:
        self.start_with(A0, B0)
        self.player.apply_prefs(Prefs(duck=30))
        self.assertEqual([("duck", 30)], self.calls())
        self.player.apply_prefs(Prefs(duck=50))
        self.assertEqual([("duck", 30), ("restore",), ("duck", 50)], self.calls())

    def test_apply_prefs_while_paused_ducks_with_new_on_resume(self) -> None:
        self.start_with(A0, B0)
        self.player.toggle_play_pause()
        self.player.apply_prefs(Prefs(duck=60))
        self.assertEqual([("duck", 30), ("restore",)], self.calls())
        self.player.toggle_play_pause()
        self.assertEqual([("duck", 30), ("restore",), ("duck", 60)], self.calls())

    # Удержание (контракт (10)): свой текст приглушает чужие потоки тем же Ducker с
    # prefs.duck; у главного, ушедшего в hold из playing, приглушение не прерывается.

    def test_hold_when_idle_ducks_and_release_restores(self) -> None:
        self.player.start()
        self.player.apply_prefs(Prefs(duck=40))
        self.player.hold()
        self.assertEqual([("duck", 40)], self.calls())
        self.assertEqual(1, len(self.refresh_timers()))
        self.player.release()
        self.assertEqual([("duck", 40), ("restore",)], self.calls())

    def test_hold_while_playing_keeps_one_duck(self) -> None:
        self.start_with(A0, B0)
        self.player.hold()
        self.assertEqual("paused", self.state().status)
        self.assertEqual([("duck", 30)], self.calls())
        self.player.release()
        self.assertEqual([("duck", 30), ("restore",)], self.calls())

    def test_no_orphan_watch_while_held(self) -> None:
        self.ducker.has_orphans = True
        self.player.start()
        self.assertEqual(1, len(self.watch_timers()))
        self.player.hold()
        self.glib.source_remove.assert_any_call(42)
        self.player.release()
        self.assertEqual(2, len(self.watch_timers()))

    def test_shutdown_while_held_restores(self) -> None:
        self.player.start()
        self.player.hold()
        self.player.shutdown()
        self.assertEqual([("duck", 30), ("restore",)], self.calls())


class PlayerHoldTest(PlayerCase):
    """Удержание (контракт (10)): пока звучит свой текст, главный плеер на паузе и сам
    ничего не начинает; release статус не меняет."""

    def test_hold_while_playing_pauses_at_position(self) -> None:
        self.start_with(A0, B0)
        self.playback.at = 0.2
        self.player.hold()
        state = self.state()
        self.assertEqual("paused", state.status)
        self.assertAlmostEqual(0.2, state.position_s)
        self.assertIsNone(self.playback.playing)
        self.assertEqual(self.states[-1], state)

    def test_hold_when_idle_keeps_status(self) -> None:
        self.player.start()
        self.player.hold()
        self.assertEqual("idle", self.state().status)

    def test_new_message_in_idle_does_not_start_while_held(self) -> None:
        self.player.start()
        self.player.hold()
        self.arrive(A0)
        self.assertEqual("idle", self.state().status)
        self.assertEqual([], self.playback.played)

    def test_message_end_does_not_advance_while_held(self) -> None:
        self.start_with(A0, B0)
        self.player.hold()
        self.player.toggle_play_pause()  # пользователь сам продолжил главный
        self.player._on_entry(self.joined(seconds=SECONDS))
        self.finish()
        state = self.state()
        self.assertEqual(("stopped", "bb"), (state.status, self.task()))
        self.assertEqual(0.0, state.position_s)
        self.assertEqual([A0, A0], self.playback.played[:2])
        self.assertNotIn(B0, self.playback.played)

    def test_release_keeps_paused(self) -> None:
        self.start_with(A0, B0)
        self.player.hold()
        count = len(self.states)
        self.player.release()
        self.assertEqual("paused", self.state().status)
        self.assertEqual(count + 1, len(self.states))
        self.assertEqual([A0], self.playback.played)

    def test_new_message_after_release_starts_in_idle(self) -> None:
        self.player.start()
        self.player.hold()
        self.player.release()
        self.arrive(A0)
        self.assertEqual("playing", self.state().status)

    def test_user_commands_work_while_held(self) -> None:
        self.start_with(A0, B0)
        self.player.hold()
        self.player.toggle_play_pause()
        self.assertEqual("playing", self.state().status)



class PlayerHookTasksTest(PlayerCase):
    """Задача хука → задача демону и сообщение с текстом (контракты (12), (13))."""

    def hook_task(self, hook_id: str = "h1", text: str = "четыре") -> HookTask:
        fields = {"id": hook_id, "timestamp": 5.0, "session_id": "S", "project": "/w/proj",
                  "text": text, "persona": "p", "speed": 1.0, "speed_method": "tempo",
                  "voice_sherpa": "v"}
        return HookTask(hook_id, 5.0, "S", "/w/proj", text, "p", 1.0, fields)

    def manual(self) -> None:
        """Как PlayerPrefsTest.manual: плеер сам ничего не начинает."""
        self.player._starts_by_itself = lambda: False

    def test_hook_task_goes_to_daemon_as_is_and_message_appears_with_text(self) -> None:
        task = self.hook_task()
        self.daemon.ids.append("aa")
        self.player._on_hook_task(task)
        self.assertEqual([task.fields], self.daemon.puts)
        state = self.states[-1]
        [message] = state.messages
        self.assertEqual(("S", "/w/proj", "четыре", "p", 1.0, {"aa"}, [], 5.0, True),
                         (message.session_id, message.project, message.text, message.persona,
                          message.speed, message.task_ids, message.sentences,
                          message.received_at, message.sent))
        self.assertEqual(([0.0], "idle"), (state.lengths, state.status))

    def test_message_without_sentences_can_be_selected(self) -> None:
        self.manual()
        self.announce("aa")
        message = self.state().messages[0]
        self.player.play_message(message)
        self.assertEqual(("stopped", message), (self.state().status, self.state().current))

    def test_play_of_message_without_sentences_waits_for_them(self) -> None:
        self.player.start()
        self.manual()
        self.announce("aa")
        self.player.toggle_play_pause()
        self.assertEqual(("playing", 0.0, []),
                         (self.state().status, self.state().length_s, self.playback.played))
        self.arrive(A0)
        self.assertEqual([A0], self.playback.played)

    def test_logs_task_line(self) -> None:
        self.daemon.ids.append("aa")
        with self.assertLogs("tts_player.player", "INFO") as logs:
            self.player._on_hook_task(self.hook_task("t1", "четыре"))
        self.assertIn("task t1 S 6 chars", [r.getMessage() for r in logs.records])

    def test_put_failure_marks_message_not_sent_with_hook_id_and_text(self) -> None:
        text = "x" * 79 + "yz"
        with self.assertLogs("tts_player.player", "WARNING") as logs:
            self.player._on_hook_task(self.hook_task("h9", text))
        [message] = self.state().messages
        self.assertEqual((False, set()), (message.sent, message.task_ids))
        self.assertIn("h9", logs.output[0])
        self.assertIn("x" * 79 + "y", logs.output[0])
        self.assertNotIn("yz", logs.output[0])

    def test_sentences_with_task_id_join_message_and_play(self) -> None:
        self.player.start()
        self.announce("aa")
        self.assertEqual([], self.playback.played)
        self.arrive(A0)
        self.assertEqual([A0], self.playback.played)
        self.assertEqual([self.path(A0)], [s.path for s in self.state().messages[0].sentences])

    def test_unknown_and_foreign_task_sentences_are_deleted(self) -> None:
        self.player.start()
        unknown = "1000000000000000001_unknown_tts_queue_S_aa_s0.wav"
        foreign = "1000000000000000002_beef_tts_queue_S_aa_s0.wav"
        for name in (unknown, foreign):
            path = self.store_dir / name
            write_wav(path, SECONDS)
            match = NAME_PATTERN.match(name)
            with self.assertLogs("tts_player.queue", "WARNING"):
                self.player._on_sentence(Sentence(int(match["recv_ns"]), match["task_id"], "S",
                                                  "aa", 0, path))
            self.assertFalse(path.exists())
        self.assertEqual(([], []), (self.state().messages, self.playback.played))

    def test_start_takes_existing_hook_tasks_into_daemon(self) -> None:
        queue_dir = self.runtime / "hook-queue"
        queue_dir.mkdir()
        (queue_dir / "1.json").write_text('{"id": "z", "session_id": "S", "text": "xy"}')
        sessions = Sessions(self.runtime / "sessions.json")
        sessions.load()  # S из setUp
        player = Player(self.runtime, None, None, None, hook_tasks_dir=queue_dir,
                        sessions=sessions,
                        daemon_queue=self.daemon, daemon_settings=self.settings)
        self.daemon.ids.append("aa")
        player.start()
        player.shutdown()
        self.assertEqual([{"id": "z", "session_id": "S", "text": "xy"}], self.daemon.puts)
        self.assertEqual([], list(queue_dir.iterdir()))

    def test_add_and_remove_session_change_sessions_json_and_log(self) -> None:
        path = self.runtime / "sessions.json"
        with self.assertLogs("tts_player.player", "INFO") as logs:
            self.player.add_session("u1", "/p/TTSReader")
        self.assertIn("session add u1", "\n".join(logs.output))
        data = json.loads(path.read_text())
        self.assertEqual(("S", [{"id": "S", "project": "proj"},
                                {"id": "u1", "project": "TTSReader"}]),
                         (data["active"], data["sessions"]))
        with self.assertLogs("tts_player.player", "INFO") as logs:
            self.player.remove_session("u1")
        self.assertIn("session remove u1", "\n".join(logs.output))
        self.assertEqual([{"id": "S", "project": "proj"}],
                         json.loads(path.read_text())["sessions"])

    def test_start_takes_control_before_hook_tasks(self) -> None:
        control_dir = self.runtime / "tts-player" / "control"
        control_dir.mkdir(parents=True)
        (control_dir / "1.json").write_text('{"op": "add", "session": "u1", "cwd": "/p/X"}')
        queue_dir = self.runtime / "hook-queue"
        queue_dir.mkdir()
        (queue_dir / "1.json").write_text('{"id": "z", "session_id": "u1", "text": "xy"}')
        player = Player(self.runtime, None, None, None, hook_tasks_dir=queue_dir,
                        sessions=Sessions(self.runtime / "sessions2.json"),
                        control_dir=control_dir,
                        daemon_queue=self.daemon, daemon_settings=self.settings)
        self.addCleanup(player.shutdown)
        with self.assertLogs("tts_player.player", "INFO") as logs:
            player.start()
        self.assertNotIn("dropped", "\n".join(logs.output))
        self.assertEqual(["xy"], [m.text for m in player._queues["u1"].messages])
        self.assertEqual([], list(control_dir.iterdir()))

    def test_duplicate_add_and_unknown_remove_are_not_logged(self) -> None:
        self.player.add_session("u1", "/p/X")
        with self.assertNoLogs("tts_player.player", "INFO"):
            self.player.add_session("u1", "/p/X")
            self.player.remove_session("nope")

    def test_shutdown_stops_control_monitor(self) -> None:
        control_dir = self.runtime / "tts-player" / "control"
        player = Player(self.runtime, None, None, None,
                        sessions=Sessions(self.runtime / "sessions2.json"),
                        control_dir=control_dir,
                        daemon_queue=self.daemon, daemon_settings=self.settings)
        player.start()
        (control_dir / "1.json").write_text('{"op": "add", "session": "u1", "cwd": "/p/X"}')
        self.spin_real(lambda: not (control_dir / "1.json").exists())
        self.assertFalse((control_dir / "1.json").exists())
        player.shutdown()
        (control_dir / "2.json").write_text('{"op": "add", "session": "u2", "cwd": "/p/X"}')
        self.spin_real(None)
        self.assertTrue((control_dir / "2.json").exists())

    @staticmethod
    def spin_real(until) -> None:
        """Настоящий цикл GLib (в тестах плеера player.GLib подменён): до until или 300 мс."""
        loop = GLib.MainLoop()
        GLib.timeout_add(5000 if until else 300, loop.quit)
        if until:
            def check():
                if until():
                    loop.quit()
                    return False
                return True
            GLib.timeout_add(20, check)
        loop.run()


class PlayerRevoiceTest(PlayerCase):
    """Начало проигрывания сообщения с устаревшим кэшем или без звука — переозвучка (12)."""

    NEW0 = "1000000000000000010_ee_tts_queue_S_ff_s0.wav"

    def heard_a(self) -> Message:
        """A0+A1 доиграны, склейка привязана: сообщение aa прослушано, плеер idle."""
        self.start_with(A0, A1)
        self.finish()
        self.finish()
        self.player._on_entry(self.joined())
        self.assertEqual("idle", self.state().status)
        return self.state().current

    def revoice_fields(self, persona: str, speed: float, task: str = "aa") -> dict:
        return {"persona": persona, "speed": speed, "speed_method": "tempo",
                "voice_sherpa": f"voice-{persona}", "text": f"text {task}", "session_id": "S",
                "project": "proj"}

    def test_fresh_cache_plays_without_new_task(self) -> None:
        self.heard_a()
        self.player.toggle_play_pause()
        self.assertEqual(1, len(self.daemon.puts))
        self.assertEqual((JOINED, 0.0), self.playback.starts[-1])

    def test_stale_persona_revoices_with_current_persona(self) -> None:
        message = self.heard_a()
        audio = message.audio
        self.settings.persona = "q"
        self.daemon.ids.append("ee")
        self.player.toggle_play_pause()
        self.assertEqual(self.revoice_fields("q", 1.0), self.daemon.puts[-1])
        self.assertFalse(any(self.path(name).exists() for name in (A0, A1)))
        self.assertFalse(audio.exists())
        self.assertEqual(({"aa", "ee"}, "q", 1.0, [], None, False),
                         (message.task_ids, message.persona, message.speed, message.sentences,
                          message.audio, message.heard))
        self.assertEqual(("playing", 0.0), (self.state().status, self.state().position_s))
        played = list(self.playback.played)
        self.arrive(self.NEW0)
        self.assertEqual(played + [self.NEW0], self.playback.played)

    def test_stale_speed_revoices(self) -> None:
        self.heard_a()
        self.settings.speed = 1.5
        self.daemon.ids.append("ee")
        self.player.toggle_play_pause()
        self.assertEqual(self.revoice_fields("p", 1.5), self.daemon.puts[-1])

    def test_message_in_flight_is_not_revoiced(self) -> None:
        """Задача сообщения ещё синтезируется: оно играет, как растёт, без новой задачи."""
        self.start_with(A0)
        self.announce("bb")  # у второго сообщения нет предложений: «aa» ещё не завершено
        self.player.play_message(self.player._queue.messages[1])
        self.player.play_message(self.player._queue.messages[0])
        self.assertEqual("stopped", self.state().status)
        self.settings.persona = "q"
        puts = len(self.daemon.puts)
        self.player.toggle_play_pause()
        self.assertEqual(puts, len(self.daemon.puts))
        self.assertEqual(0, self.settings.reads)
        self.assertEqual((A0, 0.0), self.playback.starts[-1])

    def test_click_only_selects_and_play_revoices(self) -> None:
        self.start_with(A0, B0)
        self.finish()
        self.finish()
        self.settings.persona = "q"
        self.player.play_message(self.state().messages[0])
        self.assertEqual(2, len(self.daemon.puts))
        self.daemon.ids.append("ee")
        self.player.toggle_play_pause()
        self.assertEqual(self.revoice_fields("q", 1.0), self.daemon.puts[-1])

    def test_prev_into_stale_message_revoices(self) -> None:
        self.start_with(A0, B0)
        self.finish()
        self.settings.persona = "q"
        self.daemon.ids.append("ee")
        self.player.prev()
        self.assertEqual(self.revoice_fields("q", 1.0), self.daemon.puts[-1])
        self.assertEqual("ee", self.task())

    def test_autoplay_transition_revoices_stale_next(self) -> None:
        self.start_with(A0, B0)
        self.finish()
        self.finish()
        self.settings.persona = "q"
        self.daemon.ids.append("ee")
        self.player.play_message(self.state().messages[0])
        self.player.toggle_play_pause()
        self.arrive(self.NEW0)
        self.player._on_entry(self.joined(seconds=SECONDS))
        self.daemon.ids.append("ff")
        self.finish()
        self.assertEqual(self.revoice_fields("q", 1.0, "bb"), self.daemon.puts[-1])
        self.assertEqual(("playing", "ff"), (self.state().status, self.task()))

    def test_silent_message_is_skipped_by_autoplay_and_voiced_on_user_play(self) -> None:
        """Задача A не дала звука, первое предложение B пришло в idle: играет само B, автозапуск
        через A не идёт и в демон её заново не ставит (A не прослушано); play пользователя на A
        переозвучивает."""
        self.player.start()
        self.announce("aa")
        self.announce("bb")
        self.daemon.ids.append("ee")
        self.arrive(B0)
        a, b = self.state().messages
        self.assertEqual((["text aa", "text bb"], b, [B0], False),
                         ([p["text"] for p in self.daemon.puts], self.state().current,
                          self.playback.played, a.heard))
        self.player.prev()
        self.assertEqual(self.revoice_fields("p", 1.0), self.daemon.puts[-1])
        self.assertEqual((a, "ee", False), (self.state().current, self.task(), a.heard))

    def test_silent_next_message_is_skipped_at_autoplay_transition(self) -> None:
        self.start_with(A0)
        self.announce("bb")
        self.announce("cc")
        self.arrive(C0)
        self.finish()
        self.assertEqual([A0, C0], self.playback.played)
        self.assertEqual(3, len(self.daemon.puts))
        self.assertTrue(self.state().messages[1].heard)

    def test_not_sent_message_is_voiced_on_play(self) -> None:
        self.player.start()
        self.player._starts_by_itself = lambda: False
        with self.assertLogs("tts_player.player", "WARNING"):
            self.player._on_hook_task(HookTask("h1", 1.0, "S", "proj", "text aa", "p", 1.0,
                                               {"id": "h1", "text": "text aa"}))
        self.daemon.ids.append("ee")
        self.player.toggle_play_pause()
        self.assertEqual(self.revoice_fields("p", 1.0), self.daemon.puts[-1])
        message = self.state().current
        self.assertEqual(({"ee"}, True, "playing"),
                         (message.task_ids, message.sent, self.state().status))
        self.arrive(self.NEW0)
        self.assertEqual([self.NEW0], self.playback.played)

    def test_revoice_failure_skips_message_with_warning(self) -> None:
        self.heard_a()
        self.settings.persona = "q"
        with self.assertLogs("tts_player.player", "WARNING") as logs:
            self.player.toggle_play_pause()
        self.assertIn("text aa", logs.output[-1])
        message = self.state().current
        self.assertEqual((False, True, "idle"),
                         (message.sent, message.heard, self.state().status))

    def test_config_failure_keeps_cache_with_warning(self) -> None:
        self.heard_a()
        self.settings.error = SettingsError("config.json не JSON")
        with self.assertLogs("tts_player.player", "WARNING") as logs:
            self.player.toggle_play_pause()
        self.assertIn("config.json не JSON", logs.output[0])
        self.assertEqual(1, len(self.daemon.puts))
        self.assertEqual((JOINED, 0.0), self.playback.starts[-1])


class PlayerSessionsTest(PlayerCase):
    """Очереди по сессиям (вход плеера «сессии»): в демон — только задачи активной при молчащем
    своём тексте, остальное — отложенный текст в хвост своей сессии; переключение, очистка,
    завершение, перезапуск. Активна S (setUp), B — в списке."""

    B1 = "1000000000000000011_b1_tts_queue_B_b1_s0.wav"
    B2 = "1000000000000000012_b2_tts_queue_B_b2_s0.wav"
    C1 = "1000000000000000021_c1_tts_queue_C_c1_s0.wav"

    def setUp(self) -> None:
        super().setUp()
        self.player.add_session("B", "/w/other")

    def hook(self, task_id: str, session: str, player: Player | None = None) -> None:
        """Задача хука; id задачи демона не запасён: в демон она уйти не должна."""
        player = self.player if player is None else player
        player._on_hook_task(HookTask(f"hook-{task_id}", 1.0, session, "proj",
                                      f"text {task_id}", "p", 1.0,
                                      {"id": f"hook-{task_id}", "text": f"text {task_id}"}))

    def deliver(self, name: str) -> Path:
        """Предложение, забранное inbox/, без задачи хука (в отличие от arrive)."""
        path = self.store_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        write_wav(path, SECONDS)
        match = NAME_PATTERN.match(name)
        self.player._on_sentence(Sentence(int(match["recv_ns"]), match["task_id"],
                                          match["session_id"], match["token"],
                                          int(match["index"]), path))
        return path

    def messages(self, session: str) -> list[Message]:
        return self.player._queues[session].messages

    def voiced(self, task: str, session: str) -> dict:
        """Поля задачи демону для отложенного: текущий голос (p, 1.0) и текст сообщения."""
        return {"persona": "p", "speed": 1.0, "speed_method": "tempo", "voice_sherpa": "voice-p",
                "text": f"text {task}", "session_id": session, "project": "proj"}

    def b_playing(self) -> Path:
        """B выбрана, её отложенное b1 озвучено по play/pause и играет; файл предложения."""
        self.hook("b1", "B")
        self.player.select_session("B")
        self.daemon.ids.append("b1")
        self.player.toggle_play_pause()
        return self.deliver(self.B1)

    def test_task_of_active_goes_to_daemon_and_list(self) -> None:
        self.announce("aa")
        self.assertEqual(1, len(self.daemon.puts))
        [message] = self.state().messages
        self.assertEqual(({"aa"}, False), (message.task_ids, message.deferred))

    def test_task_of_inactive_is_deferred_and_not_sent(self) -> None:
        with self.assertLogs("tts_player.player", "INFO") as logs:
            self.hook("b1", "B")
        self.assertEqual([], self.daemon.puts)
        [message] = self.messages("B")
        self.assertEqual((True, False, "text b1"), (message.deferred, message.sent, message.text))
        self.assertEqual([], self.state().messages)
        self.assertIn("task hook-b1 B deferred", [r.getMessage() for r in logs.records])

    def test_task_of_unlisted_session_is_dropped(self) -> None:
        with self.assertLogs("tts_player.player", "INFO") as logs:
            self.hook("x1", "X")
        self.assertEqual([], self.daemon.puts)
        self.assertEqual(([], []), (self.messages("S"), self.messages("B")))
        self.assertNotIn("X", self.player._queues)
        self.assertEqual(["task hook-x1 X dropped: not in list"],
                         [r.getMessage() for r in logs.records])

    def test_task_of_active_while_held_is_deferred(self) -> None:
        self.player.hold()
        self.hook("s1", "S")
        self.assertEqual([], self.daemon.puts)
        [message] = self.state().messages
        self.assertTrue(message.deferred)

    def test_control_add_lying_before_task_is_taken_first(self) -> None:
        control_dir = self.runtime / "tts-player" / "control"
        control_dir.mkdir(parents=True)
        (control_dir / "1.json").write_text('{"op": "add", "session": "C", "cwd": "/p/X"}')
        player = Player(self.runtime, None, None, None,
                        sessions=Sessions(self.runtime / "sessions2.json"),
                        control_dir=control_dir,
                        daemon_queue=self.daemon, daemon_settings=self.settings)
        with self.assertLogs("tts_player.player", "INFO") as logs:
            self.hook("c1", "C", player)
        self.assertNotIn("dropped", "\n".join(logs.output))
        self.assertEqual(["text c1"], [m.text for m in player._queues["C"].messages])
        self.assertEqual([], list(control_dir.iterdir()))

    def test_select_stops_without_focus_then_play_voices_last_deferred(self) -> None:
        self.player.start()
        self.arrive(A0)
        self.hook("b1", "B")
        self.hook("b2", "B")
        b1, b2 = self.messages("B")
        with self.assertLogs("tts_player.player", "INFO") as logs:
            self.player.select_session("B")
        self.assertIn("session select B", [r.getMessage() for r in logs.records])
        state = self.state()
        self.assertEqual((None, "B", None, "idle", 0.0, [b1, b2]),
                         (self.playback.playing, state.active, state.current, state.status,
                          state.position_s, state.messages))
        self.assertFalse(self.player._queue.is_started(b1))
        self.assertEqual("B", json.loads((self.runtime / "sessions.json").read_text())["active"])
        self.daemon.ids.append("b2")
        with self.assertLogs("tts_player.player", "INFO") as logs:
            self.player.toggle_play_pause()
        self.assertIn("voice deferred B: p 1.0", [r.getMessage() for r in logs.records])
        self.assertEqual(self.voiced("b2", "B"), self.daemon.puts[-1])
        self.assertEqual(({"b2"}, False, True, "playing", True),
                         (b2.task_ids, b2.deferred, b2.sent, self.state().status, b1.deferred))
        self.deliver(self.B2)
        self.assertEqual([A0, self.B2], self.playback.played)

    def play_last_deferred_to_end(self) -> tuple[Message, Message]:
        """B копила ответы закрытой (b1, b2 отложены); папка открыта, клик по последнему b2,
        b2 озвучено и доиграло до конца (склейка другой длины — конец без неё)."""
        self.player.start()
        self.hook("b1", "B")
        self.hook("b2", "B")
        self.player.select_session("B")
        b1, b2 = self.messages("B")
        self.player.play_message(b2)
        self.daemon.ids.append("b2")
        self.player.toggle_play_pause()
        self.deliver(self.B2)
        self.finish()
        path = self.history_dir / "tts-2000000000.000-B.wav"
        write_wav(path, 3.0)
        self.player._on_entry(HistoryEntry(path, 2_000_000_000.0, "B", "proj", 3.0))
        return b1, b2

    def test_end_of_last_waits_and_does_not_go_back_to_earlier_unheard(self) -> None:
        b1, b2 = self.play_last_deferred_to_end()
        state = self.state()
        self.assertEqual(("idle", b2), (state.status, state.current))
        self.assertEqual([self.B2], self.playback.played)
        self.assertEqual([self.voiced("b2", "B")], self.daemon.puts)
        self.assertEqual((False, True), (b1.heard, b1.deferred))

    def test_new_message_after_end_plays_itself_not_earlier_unheard(self) -> None:
        b1, _ = self.play_last_deferred_to_end()
        self.daemon.ids.append("b3")
        self.hook("b3", "B")
        b3 = self.messages("B")[2]
        self.deliver("1000000000000000013_b3_tts_queue_B_b3_s0.wav")
        self.assertEqual(("playing", b3), (self.state().status, self.state().current))
        self.assertEqual("1000000000000000013_b3_tts_queue_B_b3_s0.wav", self.playback.played[-1])
        self.assertEqual((False, True), (b1.heard, b1.deferred))

    def test_autoplay_transition_to_deferred_sends_it_and_waits(self) -> None:
        self.player.start()
        self.arrive(A0)
        self.player.hold()
        self.hook("ab", "S")
        self.player.release()
        self.player.toggle_play_pause()
        self.finish()
        deferred = self.state().messages[1]
        self.daemon.ids.append("ab")
        self.player._on_entry(self.joined(seconds=SECONDS))
        self.assertEqual(self.voiced("ab", "S"), self.daemon.puts[-1])
        self.assertEqual((deferred, "playing", False, False),
                         (self.state().current, self.state().status, deferred.heard,
                          deferred.deferred))
        self.deliver("1000000000000000031_ab_tts_queue_S_ab_s0.wav")
        self.assertEqual("1000000000000000031_ab_tts_queue_S_ab_s0.wav",
                         self.playback.played[-1])

    def test_select_active_or_unknown_changes_nothing(self) -> None:
        self.start_with(A0)
        before = self.state()
        stops = self.playback.stops
        with self.assertNoLogs("tts_player.player", "INFO"):
            self.player.select_session("S")
            self.player.select_session("X")
        self.assertEqual(before, self.state())
        self.assertEqual((stops, self.path(A0)), (self.playback.stops, self.playback.playing))

    def test_close_session_stops_and_leaves_no_active(self) -> None:
        self.start_with(A0)
        with self.assertLogs("tts_player.player", "INFO") as logs:
            self.player.close_session()
        self.assertIn("session close S", [r.getMessage() for r in logs.records])
        state = self.state()
        self.assertEqual((None, None, None, "idle", []),
                         (self.playback.playing, state.active, state.current, state.status,
                          state.messages))
        self.assertIsNone(json.loads((self.runtime / "sessions.json").read_text())["active"])
        self.assertEqual(1, len(self.messages("S")))
        self.assertTrue(self.path(A0).exists())

    def test_all_closed_nothing_plays_by_itself(self) -> None:
        """Все папки закрыты: новый ответ откладывается, позднее предложение уже отправленной
        задачи не играет."""
        self.start_with(A0)
        self.player.close_session()
        puts = len(self.daemon.puts)
        self.hook("s2", "S")
        self.arrive(A1)
        self.assertEqual((None, "idle", puts), (self.playback.playing, self.state().status,
                                                len(self.daemon.puts)))
        self.assertTrue(self.messages("S")[-1].deferred)

    def test_close_session_without_active_changes_nothing(self) -> None:
        self.player.close_session()
        before = self.state()
        with self.assertNoLogs("tts_player.player", "INFO"):
            self.player.close_session()
        self.assertEqual(before, self.state())

    def test_counts_are_messages_of_each_session_in_list_order(self) -> None:
        self.start_with(A0)
        self.hook("b1", "B")
        self.hook("b2", "B")
        self.assertEqual([1, 2], self.state().counts)

    def test_tail_is_per_session(self) -> None:
        self.player.apply_prefs(Prefs(tail=1))
        self.start_with(A0)
        a = self.state().current
        self.b_playing()
        self.player.select_session("S")
        self.assertEqual([a], self.state().messages)
        self.assertTrue(self.player._queue.is_started(a))
        self.assertTrue(self.path(A0).exists())

    def test_late_sentence_of_left_session_joins_its_message_and_does_not_play(self) -> None:
        self.start_with(A0)
        self.player.select_session("B")
        self.deliver(A1)
        self.assertEqual([A0], self.playback.played)
        [a] = self.messages("S")
        self.assertEqual([A0, A1], [s.path.name for s in a.sentences])
        self.assertEqual(("idle", None), (self.state().status, self.state().current))

    def test_sentence_of_other_session_ends_waiting_current_by_common_order(self) -> None:
        """aa поставлена раньше b1: предложение b1 (сессия B) завершает ожидающее aa."""
        self.player.start()
        self.announce("aa")
        self.player.select_session("B")
        self.hook("b1", "B")
        self.daemon.ids.append("b1")
        self.player.toggle_play_pause()
        self.player.select_session("S")
        self.player.toggle_play_pause()
        self.deliver(A0)
        self.finish()
        self.assertEqual("playing", self.state().status)
        self.deliver(self.B1)
        self.assertEqual(("idle", True, [A0]),
                         (self.state().status, self.state().current.heard, self.playback.played))

    def test_deferred_with_unreadable_config_warns_not_voiced_and_is_skipped(self) -> None:
        self.hook("b1", "B")
        self.player.select_session("B")
        self.settings.error = SettingsError("config.json не JSON")
        with self.assertLogs("tts_player.player", "WARNING") as logs:
            self.player.toggle_play_pause()
        [line] = [r.getMessage() for r in logs.records]
        self.assertIn("отложенное сообщение B не озвучено", line)
        self.assertIn("config.json не JSON", line)
        self.assertNotIn("кэш считается актуальным", line)
        [b1] = self.messages("B")
        self.assertEqual(([], True, True), (self.daemon.puts, b1.heard, b1.deferred))

    def test_entry_of_session_not_in_list_is_not_attached(self) -> None:
        self.start_with(A0, A1)
        self.player.remove_session("B")
        path = self.history_dir / "tts-2000000000.000-B.wav"
        write_wav(path, 1.0)
        with self.assertLogs("tts_player.player", "INFO") as logs:
            self.player._on_entry(HistoryEntry(path, 2_000_000_000.0, "B", "proj", 1.0))
        self.assertEqual([f"для склейки {path.name} нет сессии B в списке"],
                         [r.getMessage() for r in logs.records])
        self.assertEqual([None], [m.created_at for m in self.messages("S")])

    def test_select_clears_output_failure(self) -> None:
        self.start_with(A0)
        self.playback.fail("no output", True)
        self.assertTrue(self.state().output_down)
        self.player.select_session("B")
        self.assertFalse(self.state().output_down)
        self.glib.source_remove.assert_called_with(42)

    def test_remove_session_before_start_does_not_raise(self) -> None:
        sessions = Sessions(self.runtime / "sessions.json")
        sessions.load()  # S и B из setUp
        player = Player(self.runtime, None, None, None, sessions=sessions,
                        daemon_queue=self.daemon, daemon_settings=self.settings)
        player.remove_session("B")
        player._on_hook_task(HookTask("h1", 1.0, "S", "proj", "x", "p", 1.0, {"id": "h1"}))
        self.assertEqual((["S"], ["x"]), ([s.id for s in player.state().sessions],
                                          [m.text for m in player.state().messages]))

    def test_clear_removes_only_active_session(self) -> None:
        b_file = self.b_playing()
        self.player.select_session("S")
        self.arrive(A0)
        self.player.clear()
        self.assertEqual(([], False, "S"),
                         (self.state().messages, self.path(A0).exists(), self.state().active))
        self.assertEqual(["text b1"], [m.text for m in self.messages("B")])
        self.assertTrue(b_file.exists())

    def test_remove_inactive_deletes_its_files_and_active_plays_on(self) -> None:
        b_file = self.b_playing()
        self.player.select_session("S")
        self.arrive(A0)
        self.player.remove_session("B")
        self.assertFalse(b_file.exists())
        self.assertNotIn("B", self.player._queues)
        self.assertEqual(("playing", "S", self.path(A0)),
                         (self.state().status, self.state().active, self.playback.playing))

    def test_remove_active_stops_and_late_sentences_are_foreign(self) -> None:
        self.start_with(A0)
        self.player.remove_session("S")
        state = self.state()
        self.assertEqual((None, None, "idle", [], False),
                         (self.playback.playing, state.active, state.status, state.messages,
                          self.path(A0).exists()))
        self.assertEqual(None, state.current)
        with self.assertLogs("tts_player.queue", "WARNING"):
            late = self.deliver(A1)
        self.assertFalse(late.exists())

    def test_state_lists_sessions_and_active(self) -> None:
        state = self.state()
        self.assertEqual(([Session("S", "proj"), Session("B", "other")], "S"),
                         (state.sessions, state.active))

    def test_added_session_after_removed_active_is_not_active_while_list_not_empty(self) -> None:
        self.player.remove_session("S")
        self.player.add_session("D", "/w/d")
        self.assertIsNone(self.state().active)

    def test_clear_active_then_add_from_same_cwd_makes_new_session_active(self) -> None:
        with self.assertLogs("tts_player.player", "INFO") as logs:
            self.player.clear_session("S", "/w/proj")
            self.player.add_session("C", "/w/proj")
        state = self.state()
        self.assertEqual(("C", "idle", None, None),
                         (state.active, state.status, state.current, self.playback.playing))
        self.assertEqual(["session clear S", "session add C", "session replace C"],
                         [m.split(":", 2)[2] for m in logs.output])

    def test_clear_active_then_add_from_other_cwd_waits_for_same_cwd(self) -> None:
        self.player.clear_session("S", "/w/proj")
        self.player.add_session("C", "/w/q")
        self.assertIsNone(self.state().active)
        self.player.add_session("D", "/w/proj")
        self.assertEqual("D", self.state().active)

    def test_replacement_is_one_shot(self) -> None:
        self.player.clear_session("S", "/w/proj")
        self.player.add_session("C", "/w/proj")
        self.player.add_session("D", "/w/proj")
        self.assertEqual("C", self.state().active)

    def test_clear_inactive_keeps_active_and_sets_no_replacement(self) -> None:
        self.player.clear_session("B", "/w/other")
        self.player.add_session("C", "/w/other")
        self.assertEqual("S", self.state().active)
        self.assertNotIn("B", self.player._queues)

    def test_select_session_drops_pending_replacement(self) -> None:
        self.player.clear_session("S", "/w/proj")
        self.player.select_session("B")
        self.player.add_session("C", "/w/proj")
        self.assertEqual("B", self.state().active)

    def test_remove_active_sets_no_replacement(self) -> None:
        self.player.remove_session("S")
        self.player.add_session("C", "/w/proj")
        self.assertIsNone(self.state().active)

    def test_clear_unknown_session_does_nothing_and_logs_nothing(self) -> None:
        with self.assertNoLogs("tts_player.player", "INFO"):
            self.player.clear_session("nope", "/w/proj")
        self.player.add_session("C", "/w/proj")
        self.assertEqual("S", self.state().active)

    def test_first_session_in_empty_list_stays_closed(self) -> None:
        self.player.remove_session("S")
        self.player.remove_session("B")
        self.player.add_session("D", "/w/d")
        self.hook("dd", "D")
        self.assertEqual((None, [], ["text dd"]),
                         (self.state().active, self.daemon.puts,
                          [m.text for m in self.messages("D")]))

    def test_restart_restores_messages_by_session_and_drops_unlisted(self) -> None:
        self.player.add_session("C", "/w/c")
        self.player.select_session("C")
        self.announce("c1", "C")
        c_file = self.deliver(self.C1)
        self.player.select_session("S")
        self.player.start()
        self.arrive(A0)
        self.hook("b1", "B")
        self.player.shutdown()
        self.assertTrue(c_file.exists())
        sessions = Sessions(self.runtime / "sessions.json")
        sessions.load()
        sessions.remove("C")
        with self.assertLogs("tts_player", "INFO"):
            self.restart()
        self.assertEqual(["text aa"], [m.text for m in self.messages("S")])
        [b1] = self.messages("B")
        self.assertEqual(("text b1", True, False), (b1.text, b1.deferred, b1.sent))
        self.assertNotIn("C", self.player._queues)
        self.assertFalse(c_file.exists())
        self.assertTrue(self.path(A0).exists())
        self.assertEqual(("idle", []), (self.state().status, self.playback.played))

    @staticmethod
    def s_name(index: int, task: str) -> str:
        """Предложение задачи task сессии S, пришедшее после A0…C0."""
        return f"10000000000000001{index:02d}_{task}_tts_queue_S_{task}_s0.wav"

    def paused_with(self, *tasks: str) -> list[Path]:
        """S открыта, aa на паузе (фокус); пока стоит, приходят ответы tasks со звуком."""
        self.start_with(A0)
        self.player.toggle_play_pause()
        names = [self.s_name(i, task) for i, task in enumerate(tasks, start=1)]
        for name in names:
            self.arrive(name)
        return [self.path(name) for name in names]

    def texts(self, session: str) -> list[str]:
        return [m.text for m in self.messages(session)]

    def end_current(self) -> None:
        """Склейка S другой длины: текущее, доигранное по предложениям, кончается без неё."""
        path = self.history_dir / JOINED
        write_wav(path, 3.0)
        self.player._on_entry(HistoryEntry(path, 2_000_000_000.0, "S", "proj", 3.0))

    def finished_aa(self, tail: int) -> None:
        """S открыта, tail задан, aa доиграно: idle, текущее — aa."""
        self.player.apply_prefs(Prefs(tail=tail))
        self.start_with(A0)
        self.finish()
        self.end_current()
        self.assertEqual(("idle", "aa"), (self.state().status, self.task()))

    def test_closed_session_keeps_last_tail_answers(self) -> None:
        """Закрытая B, tail 2, 4 ответа: остаются 2 последних; у озвученного старого удалены
        файл и текст, у отложенного — текст."""
        self.player.apply_prefs(Prefs(tail=2))
        self.player.start()
        b_file = self.b_playing()
        self.player.close_session()
        self.hook("b2", "B")
        self.assertEqual(["text b1", "text b2"], self.texts("B"))
        self.hook("b3", "B")
        self.hook("b4", "B")
        self.assertEqual(["text b3", "text b4"], self.texts("B"))
        self.assertFalse(b_file.exists())
        self.assertEqual([0, 2], self.state().counts)

    def test_open_session_accumulates_with_focus_and_idle_trims_to_tail(self) -> None:
        """На паузе ответы сверх tail копятся; доиграли последний — idle, очередь режется до
        tail, доигранное текущее остаётся; новый ответ в idle режет дальше."""
        self.player.apply_prefs(Prefs(tail=2))
        s1, s2, s3 = self.paused_with("e1", "e2", "e3")
        self.assertEqual(["text aa", "text e1", "text e2", "text e3"], self.texts("S"))
        self.assertTrue(all(p.exists() for p in (self.path(A0), s1, s2, s3)))
        last = self.messages("S")[-1]
        self.player.play_message(last)
        self.player.toggle_play_pause()
        self.finish()
        self.assertEqual(("playing", 4), (self.state().status, len(self.messages("S"))))
        self.end_current()
        self.assertEqual(("idle", last), (self.state().status, self.state().current))
        self.assertEqual(["text e2", "text e3"], self.texts("S"))
        self.assertEqual([False, False, True, True],
                         [p.exists() for p in (self.path(A0), s1, s2, s3)])
        self.arrive(self.s_name(4, "e4"))
        self.assertEqual(["text e2", "text e3", "text e4"], self.texts("S"))
        self.finish()
        self.end_current()
        self.assertEqual(("idle", "e4"), (self.state().status, self.task()))
        self.assertEqual(["text e3", "text e4"], self.texts("S"))

    def test_tail_one_new_answer_after_finished_current_plays(self) -> None:
        """tail 1: доигранное текущее места не занимает — новый ответ остаётся и играет."""
        self.finished_aa(tail=1)
        self.announce("bb")
        self.assertEqual(["text aa", "text bb"], self.texts("S"))
        self.arrive(B0)
        self.assertEqual(("playing", "bb"), (self.state().status, self.task()))
        self.assertEqual([A0, B0], self.playback.played)
        self.finish()
        self.end_current()
        self.assertEqual(["text bb"], self.texts("S"))

    def test_deferred_answer_after_finished_current_keeps_both(self) -> None:
        """tail 1, звучит свой текст: ответ отложен (фокуса нет), но и он, и старшее текущее
        остаются — текущее сверх tail."""
        self.finished_aa(tail=1)
        self.player.hold()
        self.hook("ab", "S")
        self.assertEqual(["text aa", "text ab"], self.texts("S"))
        self.assertIs(self.messages("S")[0], self.state().current)

    def test_two_answers_before_first_sentence_both_play_in_order(self) -> None:
        """tail 2, текущее доиграно, два ответа подряд до первого предложения: остаются оба и
        играют по порядку."""
        self.finished_aa(tail=2)
        self.announce("e1")
        self.announce("e2")
        self.assertEqual(["text aa", "text e1", "text e2"], self.texts("S"))
        e1, e2 = self.s_name(1, "e1"), self.s_name(2, "e2")
        self.arrive(e1)
        self.arrive(e2)
        self.finish()
        self.assertEqual([A0, e1, e2], self.playback.played)

    def test_burst_of_answers_in_idle_open_session_is_kept_then_trimmed(self) -> None:
        """tail 2, три ответа подряд в открытой сессии без текущего: ушедшие в синтез и ждущие
        автоперехода — фокус, остаются все и играют по порядку; прослушаны, idle — до tail."""
        self.player.apply_prefs(Prefs(tail=2))
        self.player.start()
        for task in ("e1", "e2", "e3"):
            self.announce(task)
        self.assertEqual(["text e1", "text e2", "text e3"], self.texts("S"))
        names = [self.s_name(i, task) for i, task in enumerate(("e1", "e2", "e3"), start=1)]
        for name in names:
            self.arrive(name)
        self.finish()
        self.finish()
        self.finish()
        self.end_current()
        self.assertEqual(names, self.playback.played)
        self.assertEqual(("idle", "e3"), (self.state().status, self.task()))
        self.assertEqual(["text e2", "text e3"], self.texts("S"))

    def test_switching_folders_trims_left_and_opened_queues(self) -> None:
        """Ушли из S на паузе — она закрыта, без фокуса: режется. Открытая B (накопленное
        сверх tail) режется при открытии."""
        self.player.apply_prefs(Prefs(tail=2))
        files = self.paused_with("e1", "e2")
        queue_b = self.player._queues["B"]
        for task in ("b1", "b2", "b3"):
            queue_b.append(Message("B", text=f"text {task}", sent=False, deferred=True))
        self.player.select_session("B")
        self.assertEqual(["text e1", "text e2"], self.texts("S"))
        self.assertFalse(self.path(A0).exists())
        self.assertTrue(all(p.exists() for p in files))
        self.assertEqual(["text b2", "text b3"], self.texts("B"))

    def test_closing_paused_folder_trims_its_queue(self) -> None:
        self.player.apply_prefs(Prefs(tail=2))
        s1, s2, s3 = self.paused_with("e1", "e2", "e3")
        self.player.close_session()
        self.assertEqual(["text e2", "text e3"], self.texts("S"))
        self.assertEqual([False, False, True, True],
                         [p.exists() for p in (self.path(A0), s1, s2, s3)])

    def test_start_trims_restored_queues_to_tail(self) -> None:
        """messages.json: у закрытой S 7 сообщений при tail 5 — после старта 5 последних."""
        tasks = [f"e{i}" for i in range(1, 7)]
        self.paused_with(*tasks)
        self.assertEqual(7, len(self.messages("S")))
        self.player.shutdown()
        sessions = Sessions(self.runtime / "sessions.json")
        sessions.load()
        sessions.deselect()
        self.restart()
        self.assertEqual([f"text {task}" for task in tasks[1:]], self.texts("S"))

    def test_start_restores_open_session_waiting_then_new_answer_plays(self) -> None:
        """Открытая S восстановлена с 7 непрослушанными при tail 5, последнее — растущее
        (склейки нет): ушло в демон в прошлом запуске, фокуса не даёт. Сама ничего не играет —
        idle без фокуса, режется до 5; новый ответ играет сам."""
        tasks = [f"e{i}" for i in range(1, 7)]
        self.paused_with(*tasks)
        self.player.shutdown()
        self.restart()
        state = self.state()
        self.assertEqual(("S", "idle", None, []),
                         (state.active, state.status, state.current, self.playback.starts))
        self.assertEqual([f"text {task}" for task in tasks[1:]], self.texts("S"))
        self.arrive(self.s_name(7, "e7"))
        self.assertEqual(("playing", "e7"), (self.state().status, self.task()))
        self.assertEqual([self.s_name(7, "e7")], self.playback.played)

    def test_smaller_tail_trims_unfocused_queues_at_once(self) -> None:
        """tail 5 → 2: закрытая B режется сразу, S на паузе (фокус) — нет."""
        self.paused_with("e1", "e2", "e3")
        for task in ("b1", "b2", "b3"):
            self.hook(task, "B")
        self.player.apply_prefs(Prefs(tail=2))
        self.assertEqual(["text b2", "text b3"], self.texts("B"))
        self.assertEqual(4, len(self.messages("S")))


A2 = "1000000000000000006_aa_tts_queue_S_aa_s2.wav"


class PlayerSentenceCountTest(PlayerCase):
    """Конец сообщения по числу предложений (SentenceCounter): сообщение завершено, когда у его
    задачи пришли все индексы 0…n−1, — без склейки и без предложения следующей задачи."""

    def setUp(self) -> None:
        super().setUp()
        self.counter = FakeCounter()
        self.ducker = FakeDucker()
        self.player = self.make_player(Sessions(self.runtime / "sessions.json"))
        self.player.add_session("S", "/w/proj")
        self.player.select_session("S")
        self.player.start()

    def make_player(self, sessions: Sessions) -> Player:
        player = Player(self.runtime, None, self.history_dir, self.runtime / "handy_analysis.db",
                        ducker=self.ducker, sessions=sessions, daemon_queue=self.daemon,
                        daemon_settings=self.settings, sentence_counter=self.counter)
        self.playback = player._playback
        player.on_change = self.states.append
        return player

    def calls(self) -> list[tuple]:
        return [c for c in self.ducker.calls if c != ("refresh",)]

    def test_hook_task_asks_count_of_its_text_once(self) -> None:
        self.announce("aa")
        self.assertEqual(["text aa"], [text for text, _ in self.counter.requests])
        self.counter.answer(2)
        self.assertEqual(2, self.state().messages[0].sentence_count)

    def test_single_sentence_ends_when_joined_file_came_before_it(self) -> None:
        """Случай из журнала: склейка пришла раньше, чем inbox/ отдал предложение, — «для
        склейки … нет сообщения сессии», и сообщение без счётчика оставалось «растущим» с
        приглушением. С числом предложений оно кончается сразу после своего предложения."""
        self.announce("aa")
        self.counter.answer(1)
        with self.assertLogs("tts_player.queue", "INFO") as logs:
            self.player._on_entry(self.joined(seconds=SECONDS))
        self.assertIn(f"для склейки {JOINED} нет сообщения сессии S", logs.output[0])
        self.arrive(A0)
        self.assertEqual([A0], self.playback.played)
        self.finish()
        state = self.state()
        self.assertEqual("idle", state.status)
        self.assertTrue(state.current.heard)
        self.assertEqual([("duck", 30), ("restore",)], self.calls())

    def test_long_message_ends_at_last_index(self) -> None:
        self.announce("aa")
        self.counter.answer(3)
        for name in (A0, A1, A2):
            self.arrive(name)
        for _ in range(3):
            self.finish()
        self.assertEqual([A0, A1, A2], self.playback.played)
        self.assertEqual("idle", self.state().status)
        self.assertTrue(self.state().current.heard)
        self.assertEqual([("duck", 30), ("restore",)], self.calls())

    def test_count_after_last_sentence_ends_waiting_message(self) -> None:
        """Ответ счётчика асинхронный: пришёл, когда сообщение уже доиграно и ждёт, — конец."""
        self.announce("aa")
        self.arrive(A0)
        self.finish()
        self.assertEqual("playing", self.state().status)
        self.counter.answer(1)
        self.assertEqual("idle", self.state().status)
        self.assertTrue(self.state().current.heard)
        self.assertEqual([("duck", 30), ("restore",)], self.calls())

    def test_count_of_unfinished_message_keeps_waiting(self) -> None:
        self.announce("aa")
        self.arrive(A0)
        self.finish()
        self.counter.answer(2)
        self.assertEqual("playing", self.state().status)
        self.arrive(A1)
        self.finish()
        self.assertEqual("idle", self.state().status)

    def test_unavailable_counter_keeps_old_signals(self) -> None:
        """Счётчик не ответил: доигранное сообщение ждёт, конец — по склейке."""
        self.announce("aa")
        self.arrive(A0)
        self.finish()
        self.assertEqual("playing", self.state().status)
        self.assertEqual([("duck", 30)], self.calls())
        self.player._on_entry(self.joined(seconds=SECONDS))
        self.assertEqual("idle", self.state().status)
        self.assertEqual([("duck", 30), ("restore",)], self.calls())

    def test_shutdown_stops_counter_and_keeps_count(self) -> None:
        self.announce("aa")
        self.counter.answer(3)
        self.player.shutdown()
        self.assertTrue(self.counter.stopped)
        self.assertEqual([3], [m["sentence_count"]
                               for m in json.loads(self.saved_file().read_text())["messages"]])

    def test_restored_message_without_count_asks_counter(self) -> None:
        """Сообщения прошлого запуска с числом не пересчитываются; без числа (счётчик был
        недоступен) — спрашиваются при старте."""
        self.announce("aa")
        self.announce("bb")
        self.counter.answer(1)
        self.player.shutdown()
        self.counter = FakeCounter()
        sessions = Sessions(self.runtime / "sessions.json")
        sessions.load()
        self.player = self.make_player(sessions)
        self.player.start()
        self.assertEqual(["text bb"], [text for text, _ in self.counter.requests])
        self.counter.answer(2)
        self.assertEqual([1, 2], [m.sentence_count for m in self.state().messages])


if __name__ == "__main__":
    unittest.main()
