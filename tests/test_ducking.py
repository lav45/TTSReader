"""Тесты Ducker: заглушка pactl первой в PATH, настоящий pactl не вызывается никогда."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tts_player.ducking import Ducker

OWN_PID = 100


def sink_input(index, pid, values):
    channels = ["front-left", "front-right"] if len(values) == 2 else ["mono"]
    return {
        "index": index,
        "volume": {ch: {"value": v, "value_percent": "x", "db": "x"} for ch, v in zip(channels, values)},
        "properties": {"application.process.id": str(pid), "application.name": "app"},
    }


STUB = """#!/bin/sh
echo "$LC_ALL|$*" >> "$PACTL_LOG"
[ -n "$PACTL_SLEEP" ] && exec sleep "$PACTL_SLEEP"
[ -n "$PACTL_EXIT" ] && exit "$PACTL_EXIT"
[ "$1" = "-f" ] && cat "$PACTL_FIXTURE"
exit 0
"""


class DuckerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "pactl"
        stub.write_text(STUB)
        stub.chmod(0o755)
        self.log = root / "pactl.log"
        self.fixture = root / "fixture.json"
        self.state = root / "duck.json"
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "PACTL_LOG": str(self.log),
            "PACTL_FIXTURE": str(self.fixture),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.set_inputs([sink_input(1, 200, [60000, 50000]), sink_input(2, OWN_PID, [65536, 65536]),
                         sink_input(3, 300, [40000])])
        self.ducker = Ducker(self.state, OWN_PID)

    def set_inputs(self, inputs):
        self.fixture.write_text(json.dumps(inputs))

    def calls(self):
        if not self.log.exists():
            return []
        return [line.split("|", 1) for line in self.log.read_text().splitlines()]

    def set_calls(self):
        return [argv.split() for _, argv in self.calls() if argv.startswith("set-sink-input-volume")]

    def clear_log(self):
        self.log.write_text("")

    def test_duck_lowers_foreign_only(self):
        self.ducker.duck(30)
        self.assertEqual(self.set_calls(), [
            ["set-sink-input-volume", "1", "18000", "15000"],
            ["set-sink-input-volume", "3", "12000"],
        ])
        self.assertTrue(self.ducker.ducked)
        self.assertTrue(self.state.exists())

    def test_lc_all_c(self):
        self.ducker.duck(30)
        self.assertTrue(all(env == "C" for env, _ in self.calls()))
        self.assertIn("-f json list sink-inputs", [a for _, a in self.calls()])

    def test_restore(self):
        self.ducker.duck(30)
        self.set_inputs([sink_input(1, 200, [18000, 15000]), sink_input(2, OWN_PID, [1, 1])])
        self.clear_log()
        self.ducker.restore()
        self.assertEqual(self.set_calls(), [["set-sink-input-volume", "1", "60000", "50000"]])
        self.assertFalse(self.state.exists())
        self.assertFalse(self.ducker.ducked)

    def test_refresh_only_new(self):
        self.ducker.duck(50)
        self.set_inputs([sink_input(1, 200, [30000, 25000]), sink_input(4, 400, [20000, 20000])])
        self.clear_log()
        self.ducker.refresh()
        self.assertEqual(self.set_calls(), [["set-sink-input-volume", "4", "10000", "10000"]])
        saved = json.loads(self.state.read_text())
        self.assertIn("4", json.dumps(saved))

    def test_duck_twice_acts_as_refresh(self):
        self.ducker.duck(50)
        self.clear_log()
        self.ducker.duck(50)
        self.assertEqual(self.set_calls(), [])

    def test_duck_100_noop(self):
        self.ducker.duck(100)
        self.assertEqual(self.set_calls(), [])
        self.assertFalse(self.ducker.ducked)

    def test_recover(self):
        self.ducker.duck(30)
        fresh = Ducker(self.state, OWN_PID)
        self.set_inputs([sink_input(1, 200, [18000, 15000])])
        self.clear_log()
        fresh.recover()
        self.assertEqual(self.set_calls(), [["set-sink-input-volume", "1", "60000", "50000"]])
        self.assertFalse(self.state.exists())

    def test_recover_corrupt(self):
        self.state.write_text("{не json")
        with self.assertLogs("tts_player.ducking", "WARNING"):
            self.ducker.recover()
        self.assertFalse(self.state.exists())
        self.assertEqual(self.set_calls(), [])

    def test_pactl_failures_do_not_raise(self):
        cases = [{"PACTL_EXIT": "1"}, {"PACTL_FIXTURE": str(self.state.parent / "bad")}, {"PACTL_SLEEP": "3"}]
        (self.state.parent / "bad").write_text("not json")
        for extra in cases:
            with self.subTest(extra=extra), mock.patch.dict(os.environ, extra):
                started = time.monotonic()
                with self.assertLogs("tts_player.ducking", "WARNING"):
                    self.ducker.duck(30)
                self.assertLess(time.monotonic() - started, 2.8)
                self.assertFalse(self.ducker.ducked)

    def test_no_pactl(self):
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            with self.assertLogs("tts_player.ducking", "WARNING"):
                self.ducker.duck(30)


if __name__ == "__main__":
    unittest.main()
