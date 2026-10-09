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
        self.now = 1000.0
        self.ducker = Ducker(self.state, OWN_PID, clock=lambda: self.now)

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
        self.now += Ducker.MIN_AGE_S
        self.ducker.refresh()
        self.assertEqual(self.set_calls(), [["set-sink-input-volume", "4", "10000", "10000"]])
        saved = json.loads(self.state.read_text())
        self.assertIn("4", saved["inputs"])
        self.assertEqual(saved["inputs"]["4"]["volumes"], [20000, 20000])

    def test_duck_twice_acts_as_refresh(self):
        self.ducker.duck(50)
        self.set_inputs([sink_input(1, 200, [30000, 25000]), sink_input(5, 500, [10000])])
        self.ducker.duck(50)
        self.now += Ducker.MIN_AGE_S
        self.clear_log()
        self.ducker.duck(50)
        self.assertEqual(self.set_calls(), [["set-sink-input-volume", "5", "5000"]])

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
        self.assertFalse(self.ducker.ducked)

    def test_restore_malformed_item(self):
        self.ducker.duck(30)
        self.set_inputs([{"no": "index"}, "junk", sink_input(1, 200, [18000, 15000])])
        self.clear_log()
        self.ducker.restore()
        self.assertEqual(self.set_calls(), [["set-sink-input-volume", "1", "60000", "50000"]])

    def test_recover_malformed_item(self):
        self.ducker.duck(30)
        self.set_inputs([None, {"index": 1}, sink_input(1, 200, [18000, 15000])])
        self.clear_log()
        Ducker(self.state, OWN_PID).recover()
        self.assertEqual(self.set_calls(), [["set-sink-input-volume", "1", "60000", "50000"]])

    def test_restore_list_failure_is_blind(self):
        self.ducker.duck(30)
        self.clear_log()
        with mock.patch.dict(os.environ, {"PACTL_FIXTURE": str(self.state.parent / "bad")}):
            (self.state.parent / "bad").write_text("not json")
            with self.assertLogs("tts_player.ducking", "WARNING"):
                self.ducker.restore()
        self.assertEqual(self.set_calls(), [
            ["set-sink-input-volume", "1", "60000", "50000"],
            ["set-sink-input-volume", "3", "40000"],
        ])
        self.assertFalse(self.state.exists())

    def test_recover_list_failure_is_blind(self):
        self.ducker.duck(30)
        self.clear_log()
        with mock.patch.dict(os.environ, {"PACTL_EXIT": "1"}):
            with self.assertLogs("tts_player.ducking", "WARNING"):
                Ducker(self.state, OWN_PID).recover()
        attempted = [argv for _, argv in self.calls() if argv.startswith("set-sink-input-volume")]
        self.assertEqual(attempted, ["set-sink-input-volume 1 60000 50000", "set-sink-input-volume 3 40000"])
        self.assertFalse(self.state.exists())

    def test_restore_not_ducked_no_pactl(self):
        self.state.write_text("{}")
        self.ducker.restore()
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.state.exists())

    def test_restore_skips_reused_index(self):
        self.ducker.duck(30)
        other = sink_input(1, 999, [11111, 22222])
        other["properties"]["application.name"] = "other"
        self.set_inputs([other, sink_input(3, 300, [12000])])
        self.clear_log()
        self.ducker.restore()
        self.assertEqual(self.set_calls(), [["set-sink-input-volume", "3", "40000"]])

    def test_recover_skips_reused_index(self):
        self.ducker.duck(30)
        self.set_inputs([sink_input(1, 999, [11111, 22222]), sink_input(3, 300, [12000])])
        self.clear_log()
        Ducker(self.state, OWN_PID).recover()
        self.assertEqual(self.set_calls(), [["set-sink-input-volume", "3", "40000"]])

    def test_refresh_treats_reused_index_as_new(self):
        self.ducker.duck(50)
        self.set_inputs([sink_input(1, 999, [20000, 20000]), sink_input(3, 300, [20000])])
        self.ducker.refresh()
        self.now += Ducker.MIN_AGE_S
        self.clear_log()
        self.ducker.refresh()
        self.assertEqual(self.set_calls(), [["set-sink-input-volume", "1", "10000", "10000"]])
        self.assertEqual(json.loads(self.state.read_text())["inputs"]["1"]["volumes"], [20000, 20000])

    def test_new_stream_ducked_only_after_min_age(self):
        self.ducker.duck(50)
        self.set_inputs([sink_input(1, 200, [30000, 25000]), sink_input(9, 900, [20000])])
        self.ducker.refresh()
        self.now += 4.9
        self.clear_log()
        self.ducker.refresh()
        self.assertEqual(self.set_calls(), [])
        self.assertNotIn("9", json.loads(self.state.read_text())["inputs"])
        self.set_inputs([sink_input(1, 200, [30000, 25000]), sink_input(9, 900, [24000])])
        self.now += 0.1
        self.ducker.refresh()
        self.assertEqual(self.set_calls(), [["set-sink-input-volume", "9", "12000"]])
        self.assertEqual(json.loads(self.state.read_text())["inputs"]["9"]["volumes"], [24000])

    def test_short_stream_leaves_no_trace(self):
        self.ducker.duck(50)
        self.set_inputs([sink_input(1, 200, [30000, 25000]), sink_input(9, 900, [20000])])
        self.ducker.refresh()
        self.now += 3
        self.set_inputs([sink_input(1, 200, [30000, 25000])])
        self.clear_log()
        self.ducker.refresh()
        self.now += 10
        self.set_inputs([sink_input(1, 200, [30000, 25000]), sink_input(9, 900, [20000])])
        self.ducker.refresh()
        self.assertEqual(self.set_calls(), [])
        self.assertNotIn("9", json.loads(self.state.read_text())["inputs"])

    def test_preexisting_streams_ducked_immediately(self):
        self.ducker.duck(30)
        self.assertEqual(len(self.set_calls()), 2)

    def test_restore_clears_first_seen(self):
        self.ducker.duck(50)
        self.set_inputs([sink_input(9, 900, [20000])])
        self.ducker.refresh()
        self.ducker.restore()
        self.assertEqual(self.ducker._first_seen, {})


if __name__ == "__main__":
    unittest.main()
