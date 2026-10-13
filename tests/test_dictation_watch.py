"""Тесты DictationWatch: события pactl subscribe → проверка списка потоков записи → on_change.
pactl — поддельный скрипт под текущим интерпретатором (как воркер в test_sentence_counter):
subscribe печатает строки, которые тест дописывает в файл событий; `-f json list …` отвечает
содержимым файлов теста. «Claude Code» — копия /bin/sleep в временном claude_dir."""
import tests  # noqa: F401 — окружение тестов (tests/__init__.py)
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from tts_player.dictation_watch import DictationWatch  # noqa: E402

LOOP_LIMIT_S = 10.0

# Поддельный pactl. Каждый вызов пишется в calls.log (pid и аргументы) вместе с LC_ALL.
# subscribe: печатает новые строки events.txt; строка EXIT — выход. -f json list <что>:
# содержимое <что>.json, прочитанное при запуске, после задержки из delay.txt (если есть);
# есть fail.txt — сообщение в stderr и выход 1
PACTL = '''#!{python}
import os
import sys
import time
from pathlib import Path

ROOT = Path({root!r})
with open(ROOT / "calls.log", "a", encoding="utf-8") as log:
    log.write(f"{{os.getpid()}} {{os.environ.get('LC_ALL')}} {{' '.join(sys.argv[1:])}}\\n")
if sys.argv[1:] == ["subscribe"]:
    seen = 0
    while True:
        lines = (ROOT / "events.txt").read_text().splitlines()
        for line in lines[seen:]:
            if line == "EXIT":
                sys.exit(0)
            print(line, flush=True)
        seen = len(lines)
        time.sleep(0.01)
if sys.argv[1:3] == ["-f", "json"] and sys.argv[3] == "list":
    if (ROOT / "fail.txt").exists():
        print("Connection failure", file=sys.stderr)
        sys.exit(1)
    text = (ROOT / f"{{sys.argv[4]}}.json").read_text()
    delay = ROOT / "delay.txt"
    if delay.exists():
        time.sleep(float(delay.read_text()))
    sys.stdout.write(text)
    sys.exit(0)
sys.exit(1)
'''


class DictationWatchTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.pactl = self.root / "pactl"
        self.pactl.write_text(PACTL.format(python=sys.executable, root=str(self.root)))
        self.pactl.chmod(0o755)
        (self.root / "events.txt").write_text("")
        self.claude_dir = self.root / "claude" / "versions"
        self.claude_dir.mkdir(parents=True)
        self.changes: list[bool] = []
        self.set_streams([], [])

    def make(self, pactl: Path | None = None) -> DictationWatch:
        watch = DictationWatch(self.changes.append, pactl=str(pactl or self.pactl),
                               claude_dir=self.claude_dir)
        self.addCleanup(watch.stop)
        return watch

    def process(self, directory: Path, name: str) -> int:
        """Копия /bin/sleep в directory: /proc/<pid>/exe указывает в directory."""
        directory.mkdir(parents=True, exist_ok=True)
        exe = directory / name
        shutil.copy("/bin/sleep", exe)
        child = subprocess.Popen([str(exe), "60"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return child.pid

    def set_streams(self, outputs: list[dict], clients: list[dict]) -> None:
        self.write("source-outputs.json", json.dumps(outputs))
        self.write("clients.json", json.dumps(clients))

    def write(self, name: str, text: str) -> None:
        """Атомарно: поддельный pactl не прочитает файл наполовину."""
        part = self.root / f"{name}.part"
        part.write_text(text)
        os.replace(part, self.root / name)

    def set_stream_of(self, pid: int | None) -> None:
        """Один поток записи #7 у клиента 42; pid None — потоков нет."""
        if pid is None:
            self.set_streams([], [{"index": 42, "properties": {}}])
            return
        self.set_streams([{"index": 7, "client": "42"}],
                         [{"index": 42, "properties": {"pipewire.sec.pid": str(pid)}}])

    def event(self, line: str) -> None:
        with open(self.root / "events.txt", "a", encoding="utf-8") as events:
            events.write(line + "\n")

    def calls(self) -> list[str]:
        log = self.root / "calls.log"
        if not log.exists():
            return []
        return log.read_text().splitlines()

    def list_calls(self) -> int:
        return sum(" list source-outputs" in line for line in self.calls())

    def pids(self, args: str) -> list[int]:
        return [int(line.split()[0]) for line in self.calls() if line.endswith(f" {args}")]

    def run_until(self, predicate, limit: float = LOOP_LIMIT_S) -> None:
        context = GLib.MainContext.default()
        deadline = time.monotonic() + limit
        while not predicate():
            if time.monotonic() > deadline:
                self.fail(f"не дождался; смены {self.changes}, вызовы {self.calls()}")
            if not context.iteration(False):
                time.sleep(0.005)

    def run_for(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        context = GLib.MainContext.default()
        while time.monotonic() < deadline:
            if not context.iteration(False):
                time.sleep(0.005)

    def started(self, watch: DictationWatch) -> None:
        """start() и первая проверка завершена (ответ поддельного pactl — без задержки)."""
        watch.start()
        self.run_until(lambda: self.list_calls() >= 1 and self.pids("subscribe"))
        self.run_for(0.3)

    @staticmethod
    def alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def test_claude_stream_appears_and_disappears(self) -> None:
        watch = self.make()
        self.started(watch)
        self.assertEqual([], self.changes)
        self.assertFalse(watch.active)
        self.set_stream_of(self.process(self.claude_dir, "2.1.0"))
        self.event("Event 'new' on source-output #7")
        self.run_until(lambda: self.changes)
        self.assertEqual([True], self.changes)
        self.assertTrue(watch.active)
        self.set_stream_of(None)
        self.event("Event 'remove' on source-output #7")
        self.run_until(lambda: len(self.changes) == 2)
        self.run_for(0.2)
        self.assertEqual([True, False], self.changes)
        self.assertFalse(watch.active)
        self.assertTrue(all(line.split()[1] == "C" for line in self.calls()))

    def test_stream_of_other_process_is_not_dictation(self) -> None:
        watch = self.make()
        self.started(watch)
        self.set_stream_of(self.process(self.root / "bin", "arecord"))
        self.event("Event 'new' on source-output #7")
        self.run_until(lambda: self.list_calls() == 2)
        self.run_for(0.3)
        self.assertEqual([], self.changes)
        self.assertFalse(watch.active)

    def test_change_and_other_facilities_start_no_check(self) -> None:
        watch = self.make()
        self.started(watch)
        self.set_stream_of(self.process(self.claude_dir, "2.1.0"))
        self.event("Event 'change' on source-output #7")
        self.event("Event 'new' on source #3")
        self.event("Event 'new' on sink-input #8")
        self.event("Event 'remove' on client #42")
        self.event("Event 'new' on source-output #9")  # метка: строки выше уже прочитаны
        self.run_until(lambda: self.list_calls() == 2)
        self.run_for(0.2)
        self.assertEqual(2, self.list_calls())
        self.assertEqual([True], self.changes)  # по метке: поток Claude Code есть

    def test_claude_stream_present_at_start(self) -> None:
        self.set_stream_of(self.process(self.claude_dir, "2.1.0"))
        watch = self.make()
        watch.start()
        self.run_until(lambda: self.changes)
        self.assertEqual([True], self.changes)
        self.assertTrue(watch.active)

    def test_client_without_pid_and_vanished_process_are_not_dictation(self) -> None:
        gone = subprocess.Popen(["true"])
        gone.wait()
        self.set_streams(
            [{"index": 7, "client": "42"}, {"index": 8, "client": "43"},
             {"index": 9, "client": "99"}],
            [{"index": 42, "properties": {}},
             {"index": 43, "properties": {"pipewire.sec.pid": str(gone.pid)}}])
        watch = self.make()
        with self.assertNoLogs("tts_player.dictation_watch"):
            self.started(watch)
        self.assertEqual([], self.changes)
        self.assertFalse(watch.active)

    def test_missing_pactl_warns_once(self) -> None:
        watch = self.make(pactl=self.root / "no-pactl")
        with self.assertLogs("tts_player.dictation_watch", "WARNING") as logs:
            watch.start()
            self.run_for(0.2)
        self.assertEqual(1, len(logs.records))
        self.assertEqual([], self.changes)
        self.assertFalse(watch.active)

    def test_subscribe_exit_during_dictation_warns_and_ends_it(self) -> None:
        self.set_stream_of(self.process(self.claude_dir, "2.1.0"))
        watch = self.make()
        watch.start()
        self.run_until(lambda: self.changes)
        with self.assertLogs("tts_player.dictation_watch", "WARNING") as logs:
            self.event("EXIT")
            self.run_until(lambda: len(self.changes) == 2)
            self.run_for(0.2)
        self.assertEqual(1, len(logs.records))
        self.assertEqual([True, False], self.changes)
        self.assertFalse(watch.active)

    def test_stop_ends_subscribe_and_callbacks(self) -> None:
        watch = self.make()
        self.started(watch)
        [subscribe] = self.pids("subscribe")
        self.write("delay.txt", "1")
        self.set_stream_of(self.process(self.claude_dir, "2.1.0"))
        self.event("Event 'new' on source-output #7")
        self.run_until(lambda: self.list_calls() == 2)  # проверка в пути
        [_, listing] = self.pids("-f json list source-outputs")
        watch.stop()
        self.assertFalse(self.alive(subscribe))
        self.assertFalse(self.alive(listing))
        self.run_for(0.5)
        self.assertEqual([], self.changes)

    def list_failure_during_dictation(self, break_list) -> list[str]:
        """Диктовка идёт, break_list() ломает `pactl list`, следующая проверка не удалась:
        одна строка WARNING (она и возвращается), диктовка кончилась, слежение выключено."""
        self.set_stream_of(self.process(self.claude_dir, "2.1.0"))
        watch = self.make()
        watch.start()
        self.run_until(lambda: self.changes)
        break_list()
        with self.assertLogs("tts_player.dictation_watch", "WARNING") as logs:
            self.event("Event 'remove' on source-output #7")
            self.run_until(lambda: len(self.changes) == 2)
            self.event("Event 'new' on source-output #7")
            self.run_for(0.3)
        self.assertEqual(1, len(logs.records))
        self.assertEqual([True, False], self.changes)
        self.assertFalse(watch.active)
        self.assertEqual(2, self.list_calls())  # слежение выключено: новое событие без проверки
        return logs.output

    def test_list_exit_failure_during_dictation_warns_and_ends_it(self) -> None:
        [warning] = self.list_failure_during_dictation(lambda: self.write("fail.txt", ""))
        self.assertIn("Connection failure", warning)

    def test_list_garbage_during_dictation_warns_and_ends_it(self) -> None:
        [warning] = self.list_failure_during_dictation(
            lambda: self.write("source-outputs.json", "not json"))
        self.assertIn("непонятный ответ", warning)

    def test_event_during_check_is_not_lost(self) -> None:
        """Первая проверка прочитала пустой список и ещё идёт; поток появился и пришло
        событие — после неё ещё одна проверка, итог по последнему списку."""
        (self.root / "delay.txt").write_text("0.3")
        watch = self.make()
        watch.start()
        self.run_until(lambda: self.list_calls() == 1)
        self.set_stream_of(self.process(self.claude_dir, "2.1.0"))
        self.event("Event 'new' on source-output #7")
        self.event("Event 'new' on source-output #8")
        self.run_until(lambda: self.changes)
        self.run_for(0.8)
        self.assertEqual([True], self.changes)
        self.assertEqual(2, self.list_calls())  # два события во время проверки — одна проверка


if __name__ == "__main__":
    unittest.main()
