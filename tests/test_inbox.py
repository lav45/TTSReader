"""Тесты Inbox: забор предложений из буфера плеера в store/ и выдача по порядку приёма."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from tts_player.inbox import Inbox  # noqa: E402
from tts_player.model import Sentence  # noqa: E402

WRAPPER = Path(__file__).resolve().parent.parent / "daemon-bin" / "paplay"
TIMEOUT_MS = 5000
QUIET_MS = 500


class InboxTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.inbox_dir = self.runtime / "tts-player" / "inbox"
        self.store_dir = self.runtime / "tts-player" / "store"
        self.got: list[Sentence] = []
        self.inbox = Inbox(self.runtime, self.got.append)
        self.addCleanup(self.inbox.stop)

    def drop(self, name, data=b"wav"):
        """Кладёт файл в inbox напрямую (как будто он лежал до start)."""
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        (self.inbox_dir / name).write_bytes(data)

    def run_paplay(self, name):
        src = self.root / name
        src.write_bytes(b"wav")
        env = {"PATH": os.environ.get("PATH", ""), "XDG_RUNTIME_DIR": str(self.runtime),
               "HOME": str(self.root)}
        result = subprocess.run([str(WRAPPER), str(src)], env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def spin(self, until=None, quiet=False):
        """Крутит GLib main loop ограниченное время: до условия или фиксированную паузу."""
        loop = GLib.MainLoop()
        GLib.timeout_add(QUIET_MS if quiet else TIMEOUT_MS, loop.quit)
        if until:
            def check():
                if until():
                    loop.quit()
                    return False
                return True
            GLib.timeout_add(20, check)
        loop.run()

    def test_backlog_delivered_by_recv_ns_not_by_name(self):
        a = "2000000000000000000_unknown_tts_queue_A_aa_s0.wav"
        b = "1000000000000000000_unknown_tts_queue_Z_bb_s1.wav"
        self.drop(a)
        self.drop(b)
        self.inbox.start()
        self.assertEqual([s.recv_ns for s in self.got],
                         [1000000000000000000, 2000000000000000000])

    def test_file_from_wrapper_arrives_in_main_loop(self):
        self.inbox.start()
        self.run_paplay("tts_queue_sess-1_ab12_s3.wav")
        self.spin(until=lambda: self.got)
        self.assertEqual(len(self.got), 1)
        self.assertEqual(self.got[0].index, 3)

    def test_taken_file_moves_to_store(self):
        name = "1000000000000000000_unknown_tts_queue_S_ab_s0.wav"
        self.drop(name)
        self.inbox.start()
        self.assertFalse((self.inbox_dir / name).exists())
        self.assertTrue((self.store_dir / name).exists())
        self.assertEqual(self.got[0].path, self.store_dir / name)

    def test_dot_names_are_ignored(self):
        self.drop(".1000000000000000000_unknown_tts_queue_S_ab_s0.wav.part")
        self.drop(".hidden")
        self.inbox.start()
        self.drop(".other.part")
        self.spin(quiet=True)
        self.assertEqual(self.got, [])
        self.assertEqual(len(list(self.inbox_dir.iterdir())), 3)

    def test_unknown_name_stays_and_warns(self):
        self.drop("garbage.wav")
        with self.assertLogs("tts_player.inbox", level="WARNING") as logs:
            self.inbox.start()
        self.assertTrue(any("garbage.wav" in line for line in logs.output))
        self.assertTrue((self.inbox_dir / "garbage.wav").exists())
        self.assertEqual(self.got, [])

    def test_fields_parsed_from_name(self):
        name = "1760000000123456789_9f3c01ab_tts_queue_sess-ion-42_0a1f_s17.wav"
        self.drop(name)
        self.inbox.start()
        self.assertEqual(self.got, [Sentence(
            recv_ns=1760000000123456789,
            task_id="9f3c01ab",
            session_id="sess-ion-42",
            token="0a1f",
            index=17,
            path=self.store_dir / name,
        )])

    def test_unknown_task_id_parsed(self):
        self.drop("1760000000123456789_unknown_tts_queue_S_ab_s0.wav")
        self.inbox.start()
        self.assertEqual(["unknown"], [s.task_id for s in self.got])

    def test_name_without_task_id_stays_and_warns(self):
        """Имя прежнего вида (до id задачи в обёртке) — не предложение: WARNING, файл в inbox."""
        name = "1760000000123456789_tts_queue_S_ab_s0.wav"
        self.drop(name)
        with self.assertLogs("tts_player.inbox", level="WARNING") as logs:
            self.inbox.start()
        self.assertTrue(any(name in line for line in logs.output))
        self.assertTrue((self.inbox_dir / name).exists())
        self.assertEqual(self.got, [])

    def test_task_id_not_hex_stays_and_warns(self):
        name = "1760000000123456789_XYZ_tts_queue_S_ab_s0.wav"
        self.drop(name)
        with self.assertLogs("tts_player.inbox", level="WARNING"):
            self.inbox.start()
        self.assertTrue((self.inbox_dir / name).exists())
        self.assertEqual(self.got, [])

    def test_file_from_wrapper_carries_task_id_from_playback_json(self):
        claude_tts = self.root / ".claude-tts"
        claude_tts.mkdir()
        (claude_tts / "playback.json").write_text('{"current_message": {"id": "0123abcd"}}')
        self.inbox.start()
        self.run_paplay("tts_queue_sess-1_ab12_s3.wav")
        self.spin(until=lambda: self.got)
        self.assertEqual([("0123abcd", "sess-1", "ab12", 3)],
                         [(s.task_id, s.session_id, s.token, s.index) for s in self.got])

    def test_store_removed_at_runtime_is_recreated(self):
        self.inbox.start()
        self.store_dir.rmdir()
        self.run_paplay("tts_queue_S_ab_s0.wav")
        self.spin(until=lambda: self.got)
        self.assertEqual(len(self.got), 1)
        self.assertTrue(self.got[0].path.exists())

    def test_callback_error_does_not_stop_backlog(self):
        first = "1000000000000000000_unknown_tts_queue_S_ab_s0.wav"
        second = "2000000000000000000_unknown_tts_queue_S_ab_s1.wav"
        self.drop(first)
        self.drop(second)

        def callback(sentence):
            self.got.append(sentence)
            if sentence.index == 0:
                raise RuntimeError("boom")

        inbox = Inbox(self.runtime, callback)
        self.addCleanup(inbox.stop)
        with self.assertLogs("tts_player.inbox", level="ERROR"):
            inbox.start()
        self.assertEqual([s.index for s in self.got], [0, 1])

    def test_store_leftovers_deleted_with_warning_before_scan(self):
        self.store_dir.mkdir(parents=True)
        (self.store_dir / "1000000000000000000_unknown_tts_queue_S_aa_s0.wav").write_bytes(b"wav")
        (self.store_dir / "1000000000000000001_unknown_tts_queue_S_aa_s1.wav").write_bytes(b"wav")
        fresh = "2000000000000000000_unknown_tts_queue_S_bb_s0.wav"
        self.drop(fresh)
        with self.assertLogs("tts_player.inbox", level="WARNING") as logs:
            self.inbox.start()
        self.assertEqual(1, len(logs.output), logs.output)
        self.assertIn("2", logs.output[0])
        self.assertEqual([fresh], [p.name for p in self.store_dir.iterdir()])
        self.assertEqual([fresh], [s.path.name for s in self.got])

    def test_put_back_moves_sentences_to_inbox_under_same_name(self):
        name = "1000000000000000000_unknown_tts_queue_S_ab_s0.wav"
        self.drop(name)
        self.inbox.start()
        self.inbox.stop()
        self.inbox.put_back(self.got)
        self.assertTrue((self.inbox_dir / name).exists())
        self.assertEqual([], list(self.store_dir.iterdir()))

    def test_directory_in_inbox_is_skipped(self):
        self.inbox_dir.mkdir(parents=True)
        (self.inbox_dir / "1000000000000000000_unknown_tts_queue_S_ab_s0.wav").mkdir()
        self.inbox.start()
        self.assertEqual(self.got, [])


if __name__ == "__main__":
    unittest.main()
