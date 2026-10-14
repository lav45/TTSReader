"""Тесты SentenceCounter: постоянный процесс-счётчик в цикле GLib. Процесс — поддельный скрипт
под текущим интерпретатором (как в test_text_speaker): настоящий интерпретатор claude-tts тестам
не нужен."""
import tests  # noqa: F401 — окружение тестов (tests/__init__.py)
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from tts_player.sentence_counter import SentenceCounter  # noqa: E402

LOOP_LIMIT_S = 10.0

# Поддельный счётчик: n — число точек в тексте, второе предложение — через пробел за первой
# точкой. MODE: count — отвечает; silent — молчит; garbage — непонятная строка; bad_rest — число
# без позиции второго предложения; exit — строка error и выход 2, как без claude_code_tts
WORKER = '''
import json
import os
import sys

MODE = {mode!r}
with open({log!r}, "a", encoding="utf-8") as log:
    log.write(f"start {{os.getpid()}}\\n")
if MODE == "exit":
    print(json.dumps({{"error": "import claude_code_tts: ModuleNotFoundError"}}), flush=True)
    sys.exit(2)
for line in sys.stdin:
    text = json.loads(line)["text"]
    if MODE == "silent":
        continue
    if MODE == "garbage":
        print("garbage", flush=True)
        continue
    count = text.count(".")
    rest_at = text.index(".") + 2 if count >= 2 and MODE != "bad_rest" else None
    print(json.dumps({{"count": count, "rest_at": rest_at}}), flush=True)
'''


class SentenceCounterTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.log = self.root / "worker.log"
        self.worker = self.root / "fake_worker.py"
        self.counts: list[tuple[str, int]] = []
        self.rests: list[int | None] = []

    def make(self, mode: str = "count", python: Path | None = None) -> SentenceCounter:
        self.worker.write_text(WORKER.format(mode=mode, log=str(self.log)))
        counter = SentenceCounter(python or Path(sys.executable), self.worker)
        self.addCleanup(counter.stop)
        return counter

    def ask(self, counter: SentenceCounter, text: str) -> None:
        counter.count(text, lambda n, rest_at: self.answered(text, n, rest_at))

    def answered(self, text: str, n: int, rest_at: int | None) -> None:
        self.counts.append((text, n))
        self.rests.append(rest_at)

    def run_until(self, predicate, limit: float = LOOP_LIMIT_S) -> None:
        context = GLib.MainContext.default()
        deadline = time.monotonic() + limit
        while not predicate():
            if time.monotonic() > deadline:
                self.fail(f"не дождался; ответы {self.counts}")
            if not context.iteration(False):
                time.sleep(0.005)

    def run_for(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        context = GLib.MainContext.default()
        while time.monotonic() < deadline:
            if not context.iteration(False):
                time.sleep(0.005)

    def starts(self) -> list[int]:
        if not self.log.exists():
            return []
        return [int(line.split()[1]) for line in self.log.read_text().splitlines()]

    @staticmethod
    def alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def test_answers_in_request_order_from_one_process(self) -> None:
        counter = self.make()
        self.assertEqual([], self.starts())  # процесс заводит первый запрос
        self.ask(counter, "Раз. Два. Три.")
        self.ask(counter, "Один.")
        self.ask(counter, "")
        self.run_until(lambda: len(self.counts) == 3)
        self.assertEqual([("Раз. Два. Три.", 3), ("Один.", 1), ("", 0)], self.counts)
        self.ask(counter, "А. Б.")
        self.run_until(lambda: len(self.counts) == 4)
        self.assertEqual(("А. Б.", 2), self.counts[-1])
        self.assertEqual([5, None, None, 3], self.rests)
        self.assertEqual(1, len(self.starts()))

    def test_count_without_rest_at_breaks_counter(self) -> None:
        """Два предложения и больше без позиции второго — непонятный ответ: сбой счётчика."""
        counter = self.make("bad_rest")
        with self.assertLogs("tts_player.sentence_counter", "WARNING") as logs:
            self.ask(counter, "Раз. Два.")
            self.run_until(lambda: "непонятная" in logs.output[0] if logs.output else False)
        self.assertEqual([], self.counts)

    def test_lone_surrogate_is_counted(self) -> None:
        """Одиночный суррогат (json.loads задачи хука его пропускает) уходит экранированным
        \\uXXXX: процесс получает ту же строку, что и демон."""
        counter = self.make()
        text = "Раз \ud83d. Два."
        self.ask(counter, text)
        self.run_until(lambda: self.counts)
        self.assertEqual([(text, 2)], self.counts)

    def test_long_text_does_not_block(self) -> None:
        """Текст больше буфера канала: запись асинхронная, цикл GLib не встаёт."""
        counter = self.make()
        text = "Слово. " * 50000
        started = time.monotonic()
        self.ask(counter, text)
        self.assertLess(time.monotonic() - started, 1.0)
        self.run_until(lambda: len(self.counts) == 1)
        self.assertEqual(50000, self.counts[0][1])

    def test_stop_ends_process(self) -> None:
        counter = self.make()
        self.ask(counter, "Раз.")
        self.run_until(lambda: self.counts)
        [pid] = self.starts()
        counter.stop()
        self.assertFalse(self.alive(pid))

    def test_stop_without_requests_starts_nothing(self) -> None:
        counter = self.make()
        counter.stop()
        self.assertEqual([], self.starts())

    def test_missing_interpreter_warns_once_and_never_answers(self) -> None:
        counter = self.make(python=self.root / "no-python")
        with self.assertLogs("tts_player.sentence_counter", "WARNING") as logs:
            self.ask(counter, "Раз.")
            self.ask(counter, "Два.")
            self.run_for(0.2)
        self.assertEqual(1, len(logs.records))
        self.assertEqual([], self.counts)

    def test_worker_exit_warns_once(self) -> None:
        counter = self.make(mode="exit")
        with self.assertLogs("tts_player.sentence_counter", "WARNING") as logs:
            self.ask(counter, "Раз.")
            self.run_until(lambda: logs.records)
            self.ask(counter, "Два.")
            self.run_for(0.3)
        self.assertEqual(1, len(logs.records))
        self.assertIn("ModuleNotFoundError", logs.output[0])
        self.assertEqual([], self.counts)
        self.assertEqual(1, len(self.starts()))  # недоступный счётчик не перезапускается

    def test_garbage_line_warns_and_ends_process(self) -> None:
        counter = self.make(mode="garbage")
        with self.assertLogs("tts_player.sentence_counter", "WARNING") as logs:
            self.ask(counter, "Раз.")
            self.run_until(lambda: logs.records)
        [pid] = self.starts()
        self.assertFalse(self.alive(pid))
        self.assertEqual([], self.counts)

    def test_no_answer_within_timeout_warns_and_ends_process(self) -> None:
        counter = self.make(mode="silent")
        counter.TIMEOUT_S = 0.3
        with self.assertLogs("tts_player.sentence_counter", "WARNING") as logs:
            self.ask(counter, "Раз.")
            started = time.monotonic()
            self.run_until(lambda: logs.records)
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        [pid] = self.starts()
        self.assertFalse(self.alive(pid))
        self.ask(counter, "Два.")
        self.run_for(0.2)
        self.assertEqual([], self.counts)
        self.assertEqual(1, len(self.starts()))

    def test_failing_callback_does_not_stop_counter(self) -> None:
        counter = self.make()

        def broken(n: int, rest_at: int | None) -> None:
            raise RuntimeError("boom")

        with self.assertLogs("tts_player.sentence_counter", "ERROR"):
            counter.count("Раз.", broken)
            self.ask(counter, "Два. Три.")
            self.run_until(lambda: self.counts)
        self.assertEqual([("Два. Три.", 2)], self.counts)


if __name__ == "__main__":
    unittest.main()
