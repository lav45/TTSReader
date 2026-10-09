"""Приёмка склейки через стык: история демона -> процесс tts_player -> строки stderr.

Склейку пишет тест, как демон: WAV копией в speech_history, затем строка в базе.
"""
import queue
import shutil
import sqlite3
import time
import unittest
import uuid
import wave
from pathlib import Path

import test_player_e2e as e2e

SECONDS = 0.5


class PlayerHistoryE2ETest(e2e.PlayerProcessCase):
    def daemon_saves(self, session: str, sentences: list[Path],
                     duration: float | None = None) -> str:
        """Склейка предложений в speech_history и строка базы; имя — как у демона."""
        created_at = time.time()
        name = f"tts-{created_at:.3f}-{session[:40]}.wav"
        joined = self.wavs / name
        frames = 0
        with wave.open(str(joined), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(e2e.RATE)
            for path in sentences:
                with wave.open(str(path), "rb") as part:
                    data = part.readframes(part.getnframes())
                    frames += part.getnframes()
                out.writeframes(data)
        if duration is None:
            duration = frames / e2e.RATE
        shutil.copy2(joined, self.history / name)
        conn = sqlite3.connect(str(self.db))
        conn.execute(
            "INSERT INTO speech_history (file_name, created_at, session_id, project,"
            " persona, text, speed, tone, duration_seconds) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, created_at, session, "proj", "p", "t", 1.0, "neutral", duration),
        )
        conn.commit()
        conn.close()
        return name

    def lines_until(self, prefix: str) -> list[str]:
        """Строки до ближайшей с префиксом включительно; таймаут — провал теста."""
        lines = []
        while True:
            try:
                line = self.lines.get(timeout=e2e.WAIT)
            except queue.Empty:
                self.fail(f"нет строки '{prefix}…' за {e2e.WAIT} с; были: {lines}")
            lines.append(line)
            if line.startswith(prefix):
                return lines

    def message(self, session: str, token: str, count: int,
                seconds: float = SECONDS) -> list[Path]:
        return [self.sentence(session, token, i, seconds=seconds) for i in range(count)]

    def test_playing_message_switches_to_joined_file_at_position(self) -> None:
        session, other = uuid.uuid4().hex, uuid.uuid4().hex
        sentences = self.message(session, "aaaaaa", 3)
        follower = self.sentence(other, "bbbbbb", 0)
        self.start_player()
        for path in [*sentences, follower]:
            self.drop(path)
        self.assertTrue(self.next_line("playing ").endswith("aaaaaa_s0.wav"))
        self.assertTrue(self.next_line("playing ").endswith("aaaaaa_s1.wav"))
        name = self.daemon_saves(session, sentences)
        self.assertEqual(f"complete {session} aaaaaa 1.50s", self.next_line("complete "))
        switched = self.next_line("switched ")
        prefix = f"switched {name} at "
        self.assertTrue(switched.startswith(prefix), switched)
        position = float(switched.removeprefix(prefix))
        self.assertGreaterEqual(position, 0.5)
        self.assertLessEqual(position, 1.0)
        self.assertTrue(self.next_line("playing ").endswith(f"{other}_bbbbbb_s0.wav"))

    def test_two_passes_of_one_task_end_as_one_message(self) -> None:
        """Обёртка с playback.json (id задачи X) кладёт два прохода синтеза, как у демона:
        проход 1 (aaaaaa) оборван паузой на s1 без записи истории, проход 2 (bbbbbb) начинает
        с того же s1; склейка — только прохода 2. Плеер играет все четыре файла одним
        сообщением по recv_ns; склейка короче сообщения — одна строка ended, без complete."""
        session = uuid.uuid4().hex
        sentences = [*self.message(session, "aaaaaa", 2),
                     self.sentence(session, "bbbbbb", 1, seconds=SECONDS),
                     self.sentence(session, "bbbbbb", 2, seconds=SECONDS)]
        self.task("0123456789abcdef")
        self.start_player()
        for path in sentences:
            self.drop(path)
        played = [self.next_line("playing ") for _ in range(2)]
        self.daemon_saves(session, sentences[2:])
        lines = self.lines_until("ended ")
        self.assertEqual(f"ended {session} aaaaaa", lines[-1])
        played += [line for line in lines if line.startswith("playing ")]
        for _ in range(len(sentences) - len(played)):
            lines += self.lines_until("playing ")
            played.append(lines[-1])
        self.assertEqual([f"_0123456789abcdef_{p.name}" for p in sentences],
                         [line[line.index("_"):] for line in played])
        self.assertEqual(1, len([line for line in lines if line.startswith("ended ")]), lines)
        self.assertEqual([], [line for line in lines if line.startswith("complete ")])
        self.assert_quiet()

    def test_joined_file_of_other_length_leaves_sentences(self) -> None:
        session = uuid.uuid4().hex
        sentences = self.message(session, "aaaaaa", 3)
        self.start_player()
        for path in sentences:
            self.drop(path)
        self.assertTrue(self.next_line("playing ").endswith("aaaaaa_s0.wav"))
        self.assertTrue(self.next_line("playing ").endswith("aaaaaa_s1.wav"))
        self.daemon_saves(session, sentences, duration=2.0)
        lines = self.lines_until("playing ")
        self.assertIn(f"ended {session} aaaaaa", lines)
        self.assertEqual([], [line for line in lines if line.startswith("switched ")])
        self.assertTrue(lines[-1].endswith("aaaaaa_s2.wav"), lines)

    def test_joined_file_of_queued_message_plays_from_start(self) -> None:
        first, queued = uuid.uuid4().hex, uuid.uuid4().hex
        long = self.sentence(first, "aaaaaa", 0, seconds=3.0)
        sentences = self.message(queued, "bbbbbb", 2)
        self.start_player()
        for path in [long, *sentences]:
            self.drop(path)
        self.assertTrue(self.next_line("playing ").endswith("aaaaaa_s0.wav"))
        name = self.daemon_saves(queued, sentences)
        self.assertEqual(f"complete {queued} bbbbbb 1.00s", self.next_line("complete "))
        lines = self.lines_until("playing ")
        self.assertEqual([], [line for line in lines if line.startswith("switched ")])
        self.assertEqual(f"playing {name}", lines[-1])
        self.assert_quiet()

    def test_history_written_before_start_gives_no_lines(self) -> None:
        session = uuid.uuid4().hex
        sentence = self.sentence(session, "aaaaaa", 0)
        self.drop(sentence)
        self.daemon_saves(session, [sentence])
        self.start_player()
        lines = self.lines_until("playing ")
        self.assertEqual(1, len(lines), lines)
        self.assertTrue(lines[0].endswith("aaaaaa_s0.wav"))
        self.assert_quiet()

    def test_missing_history_dir_warns_and_plays_without_history(self) -> None:
        self.start_player(history=self.tmp / "missing")
        warning = self.next_line("история ")
        self.assertIn(str(self.tmp / "missing"), warning)
        self.drop(self.sentence(uuid.uuid4().hex, "aaaaaa", 0))
        self.assertTrue(self.next_line("playing ").endswith("aaaaaa_s0.wav"))
        self.assertIsNone(self.proc.poll())


if __name__ == "__main__":
    unittest.main()
