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
        "properties": {"application.process.id": str(pid), "application.name": f"app{pid}"},
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
        self.orphans = root / "duck-orphans.json"
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
        self.ducker = Ducker(self.state, OWN_PID, self.orphans, clock=lambda: self.now)

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
        fresh = Ducker(self.state, OWN_PID, self.orphans)
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
                ducker = Ducker(self.state, OWN_PID, self.orphans, clock=lambda: self.now)
                with self.assertLogs("tts_player.ducking", "WARNING"):
                    ducker.duck(30)
                self.assertLess(time.monotonic() - started, 2.8)
                self.assertEqual(self.set_calls(), [])

    def test_no_pactl(self):
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            with self.assertLogs("tts_player.ducking", "WARNING"):
                self.ducker.duck(30)
        self.assertEqual(self.set_calls(), [])

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
        Ducker(self.state, OWN_PID, self.orphans).recover()
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
                Ducker(self.state, OWN_PID, self.orphans).recover()
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
        Ducker(self.state, OWN_PID, self.orphans).recover()
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

    def test_timeout_makes_refresh_noop_until_next_duck(self):
        self.ducker.duck(50)
        with mock.patch.dict(os.environ, {"PACTL_SLEEP": "3"}):
            with self.assertLogs("tts_player.ducking", "WARNING"):
                self.ducker.refresh()
        self.clear_log()
        started = time.monotonic()
        self.ducker.refresh()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(self.calls(), [])
        self.ducker.restore()
        self.ducker.duck(50)
        self.assertTrue(self.calls())

    def test_no_pactl_is_not_retried_until_next_duck(self):
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            with self.assertLogs("tts_player.ducking", "WARNING"):
                self.ducker.duck(30)
            with self.assertNoLogs("tts_player.ducking", "WARNING"):
                self.ducker.refresh()
                self.ducker.refresh()
        self.ducker.refresh()
        self.assertEqual(self.set_calls(), [])
        self.ducker.restore()
        self.ducker.duck(30)
        self.assertEqual(len(self.set_calls()), 2)

    def test_state_write_failure_does_not_lower_volumes(self):
        with mock.patch("tts_player.ducking.write_json", side_effect=OSError("диск полон")):
            with self.assertLogs("tts_player.ducking", "WARNING"):
                self.ducker.duck(30)
        self.assertEqual(self.set_calls(), [])
        self.assertEqual(self.ducker._saved, {})

    def test_non_numeric_volume_skipped(self):
        bad = sink_input(8, 800, [1, 2])
        bad["volume"]["front-left"]["value"] = "loud"
        flag = sink_input(9, 900, [1])
        flag["volume"]["mono"]["value"] = True
        self.set_inputs([bad, flag, sink_input(3, 300, [40000])])
        with self.assertLogs("tts_player.ducking", "WARNING"):
            self.ducker.duck(30)
        self.assertEqual(self.set_calls(), [["set-sink-input-volume", "3", "12000"]])

    def test_failed_duck_is_retried_by_refresh(self):
        with mock.patch.dict(os.environ, {"PACTL_EXIT": "1"}):
            with self.assertLogs("tts_player.ducking", "WARNING"):
                self.ducker.duck(30)
        self.assertEqual(self.set_calls(), [])
        self.ducker.refresh()
        self.assertEqual(len(self.set_calls()), 2)

    ORPHAN = {"app200": {"original": [60000, 50000], "ducked": [18000, 15000], "known": []}}

    def orphans_data(self):
        return json.loads(self.orphans.read_text())

    def make_orphan(self):
        """duck(30); поток 1 (app200) исчезает; restore() делает его сиротой."""
        self.ducker.duck(30)
        self.set_inputs([sink_input(3, 300, [12000])])
        self.ducker.restore()
        self.clear_log()

    def test_vanished_stream_becomes_orphan_on_restore(self):
        self.make_orphan()
        self.assertTrue(self.ducker.has_orphans)
        self.assertEqual(self.orphans_data(), self.ORPHAN)

    def test_vanished_stream_becomes_orphan_on_refresh(self):
        self.ducker.duck(30)
        self.set_inputs([sink_input(3, 300, [12000])])
        self.ducker.refresh()
        self.assertEqual(self.orphans_data(), self.ORPHAN)
        self.assertNotIn("1", json.loads(self.state.read_text())["inputs"])
        self.clear_log()
        self.ducker.restore()
        self.assertEqual(self.set_calls(), [["set-sink-input-volume", "3", "40000"]])

    def test_unlowered_stream_is_not_orphan(self):
        self.ducker.duck(30)
        self.set_inputs([sink_input(1, 200, [18000, 15000]), sink_input(9, 900, [20000])])
        self.ducker.refresh()
        self.set_inputs([sink_input(1, 200, [18000, 15000])])
        self.ducker.refresh()
        self.ducker.restore()
        orphans = self.orphans_data() if self.orphans.exists() else {}
        self.assertNotIn("app900", orphans)

    def test_nameless_stream_is_not_orphan(self):
        nameless = sink_input(1, 200, [60000, 50000])
        del nameless["properties"]["application.name"]
        self.set_inputs([nameless])
        self.ducker.duck(30)
        self.set_inputs([])
        self.ducker.restore()
        self.assertFalse(self.ducker.has_orphans)

    def test_orphan_new_stream_while_ducked_uses_original_immediately(self):
        self.make_orphan()
        self.set_inputs([sink_input(7, 200, [18000, 15000])])
        self.ducker.duck(50)
        self.assertEqual(self.set_calls(), [["set-sink-input-volume", "7", "30000", "25000"]])
        self.assertFalse(self.ducker.has_orphans)
        self.assertFalse(self.orphans.exists())
        self.assertEqual(json.loads(self.state.read_text())["inputs"]["7"]["volumes"], [60000, 50000])

    def test_orphan_refresh_ducks_new_stream_without_age_wait(self):
        self.make_orphan()
        self.set_inputs([sink_input(3, 300, [40000])])
        self.ducker.duck(50)
        self.set_inputs([sink_input(3, 300, [20000]), sink_input(7, 200, [18000, 15000])])
        self.clear_log()
        self.ducker.refresh()
        self.assertEqual(self.set_calls(), [["set-sink-input-volume", "7", "30000", "25000"]])
        self.assertFalse(self.ducker.has_orphans)

    def test_watch_sets_original_when_volume_equals_ducked(self):
        self.make_orphan()
        self.set_inputs([sink_input(7, 200, [18001, 14999])])
        self.ducker.watch()
        self.assertEqual(self.set_calls(), [["set-sink-input-volume", "7", "60000", "50000"]])
        self.assertFalse(self.ducker.has_orphans)
        self.assertFalse(self.orphans.exists())

    def test_watch_leaves_user_volume_and_drops_orphan(self):
        self.make_orphan()
        self.set_inputs([sink_input(7, 200, [40000, 40000])])
        self.ducker.watch()
        self.assertEqual(self.set_calls(), [])
        self.assertFalse(self.ducker.has_orphans)

    def test_watch_without_matching_stream_keeps_orphan(self):
        self.make_orphan()
        self.set_inputs([sink_input(3, 300, [40000])])
        self.ducker.watch()
        self.assertEqual(self.set_calls(), [])
        self.assertTrue(self.ducker.has_orphans)

    def test_watch_without_orphans_does_not_call_pactl(self):
        self.ducker.watch()
        self.assertEqual(self.calls(), [])

    def test_watch_while_ducked_acts_as_refresh(self):
        self.make_orphan()
        self.set_inputs([sink_input(3, 300, [40000])])
        self.ducker.duck(50)
        self.set_inputs([sink_input(3, 300, [20000]), sink_input(7, 200, [18000, 15000])])
        self.clear_log()
        self.ducker.watch()
        self.assertEqual(self.set_calls(), [["set-sink-input-volume", "7", "30000", "25000"]])

    def test_watch_stalled_after_timeout(self):
        self.make_orphan()
        with mock.patch.dict(os.environ, {"PACTL_SLEEP": "3"}):
            with self.assertLogs("tts_player.ducking", "WARNING"):
                self.ducker.watch()
        self.clear_log()
        self.ducker.watch()
        self.assertEqual(self.calls(), [])

    def test_orphans_survive_recreation(self):
        self.make_orphan()
        fresh = Ducker(self.state, OWN_PID, self.orphans)
        self.assertTrue(fresh.has_orphans)
        self.set_inputs([sink_input(7, 200, [18000, 15000])])
        fresh.watch()
        self.assertEqual(self.set_calls(), [["set-sink-input-volume", "7", "60000", "50000"]])

    def test_corrupt_orphans_file_removed(self):
        self.orphans.write_text("{не json")
        with self.assertLogs("tts_player.ducking", "WARNING"):
            ducker = Ducker(self.state, OWN_PID, self.orphans)
        self.assertFalse(ducker.has_orphans)
        self.assertFalse(self.orphans.exists())

    def tab(self, index, pid, values):
        item = sink_input(index, pid, values)
        item["properties"]["application.name"] = "Firefox"
        return item

    def make_tab_orphan(self):
        """Вкладки A(1) и B(5, пользователь снизил до 20000) глушатся; A кончается; B жива."""
        self.set_inputs([self.tab(1, 200, [60000, 60000]), self.tab(5, 200, [20000, 20000])])
        self.ducker.duck(30)
        self.set_inputs([self.tab(5, 200, [6000, 6000])])
        self.ducker.restore()
        self.set_inputs([self.tab(5, 200, [20000, 20000])])
        self.clear_log()

    def test_preexisting_sibling_does_not_consume_orphan_in_speech(self):
        self.make_tab_orphan()
        self.assertEqual(self.orphans_data()["Firefox"]["known"], [[5, "200"]])
        self.ducker.duck(30)
        self.ducker.restore()
        self.assertEqual(self.set_calls(), [
            ["set-sink-input-volume", "5", "6000", "6000"],
            ["set-sink-input-volume", "5", "20000", "20000"],
        ])
        self.assertTrue(self.ducker.has_orphans)

    def test_preexisting_sibling_does_not_consume_orphan_in_watch(self):
        self.make_tab_orphan()
        self.set_inputs([self.tab(5, 200, [18000, 18000])])
        self.ducker.watch()
        self.assertEqual(self.set_calls(), [])
        self.assertTrue(self.ducker.has_orphans)

    def test_watch_considers_all_candidates(self):
        self.make_tab_orphan()
        self.set_inputs([self.tab(5, 200, [18000, 18000]), self.tab(8, 200, [18000, 18000])])
        self.ducker.watch()
        self.assertEqual(self.set_calls(), [["set-sink-input-volume", "8", "60000", "60000"]])
        self.assertFalse(self.ducker.has_orphans)

    def test_new_stream_with_other_volume_drops_orphan_untouched(self):
        self.make_tab_orphan()
        self.set_inputs([self.tab(5, 200, [20000, 20000]), self.tab(8, 200, [40000, 40000])])
        self.ducker.watch()
        self.assertEqual(self.set_calls(), [])
        self.assertFalse(self.ducker.has_orphans)

    def test_new_stream_other_channel_count_drops_orphan(self):
        self.make_orphan()
        self.set_inputs([sink_input(7, 200, [18000])])
        self.ducker.watch()
        self.assertEqual(self.set_calls(), [])
        self.assertFalse(self.ducker.has_orphans)

    def test_recreated_stream_in_same_pass_is_repaired(self):
        self.set_inputs([self.tab(1, 200, [60000, 60000]), self.tab(5, 200, [20000, 20000])])
        self.ducker.duck(30)
        self.set_inputs([self.tab(5, 200, [6000, 6000]), self.tab(8, 200, [18000, 18000])])
        self.clear_log()
        self.ducker.refresh()
        self.assertEqual(self.set_calls(), [["set-sink-input-volume", "8", "18000", "18000"]])
        self.assertEqual(json.loads(self.state.read_text())["inputs"]["8"]["volumes"], [60000, 60000])
        self.assertFalse(self.ducker.has_orphans)

    def test_recreated_stream_in_same_pass_returned_after_speech(self):
        self.set_inputs([self.tab(1, 200, [60000, 60000]), self.tab(5, 200, [20000, 20000])])
        self.ducker.duck(30)
        self.set_inputs([self.tab(5, 200, [6000, 6000]), self.tab(8, 200, [18000, 18000])])
        self.ducker.restore()
        self.assertEqual(self.orphans_data()["Firefox"]["known"], [[5, "200"]])
        self.clear_log()
        self.ducker.watch()
        self.assertEqual(self.set_calls(), [["set-sink-input-volume", "8", "60000", "60000"]])

    def test_muted_preexisting_stream_not_raised_with_duck_zero(self):
        self.set_inputs([self.tab(1, 200, [60000, 60000]), self.tab(5, 200, [0, 0])])
        self.ducker.duck(0)
        self.set_inputs([self.tab(5, 200, [0, 0])])
        self.ducker.restore()
        self.clear_log()
        self.ducker.watch()
        self.assertEqual(self.set_calls(), [])

    def test_orphans_changed_at(self):
        self.assertIsNone(self.ducker.orphans_changed_at)
        self.now = 1234.0
        self.make_orphan()
        self.assertEqual(self.ducker.orphans_changed_at, 1234.0)
        self.now = 2000.0
        self.assertEqual(Ducker(self.state, OWN_PID, self.orphans, clock=lambda: self.now).orphans_changed_at, 2000.0)

    def test_restore_same_index_other_pid_only_skipped(self):
        self.ducker.duck(30)
        other = sink_input(1, 999, [11111, 22222])
        other["properties"]["application.name"] = "app200"
        self.set_inputs([other])
        self.clear_log()
        self.ducker.restore()
        self.assertEqual(self.set_calls(), [])

    def test_restore_same_index_other_name_only_skipped(self):
        self.ducker.duck(30)
        other = sink_input(1, 200, [11111, 22222])
        other["properties"]["application.name"] = "other"
        self.set_inputs([other])
        self.clear_log()
        self.ducker.restore()
        self.assertEqual(self.set_calls(), [])

    def test_refresh_same_index_only_pid_or_name_changed_is_new(self):
        for pid, name in ((999, "app200"), (200, "other")):
            with self.subTest(pid=pid, name=name):
                self.ducker.duck(50)
                changed = sink_input(1, pid, [20000, 20000])
                changed["properties"]["application.name"] = name
                self.set_inputs([changed])
                self.ducker.refresh()
                self.now += Ducker.MIN_AGE_S
                self.clear_log()
                self.ducker.refresh()
                self.assertEqual(self.set_calls(), [["set-sink-input-volume", "1", "10000", "10000"]])
                self.ducker.restore()
                self.set_inputs([sink_input(1, 200, [60000, 50000]), sink_input(3, 300, [40000])])


if __name__ == "__main__":
    unittest.main()
