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
        env = {"PATH": os.environ.get("PATH", ""), "XDG_RUNTIME_DIR": str(self.runtime)}
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
        a = "2000000000000000000_tts_queue_A_aa_s0.wav"
        b = "1000000000000000000_tts_queue_Z_bb_s1.wav"
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
        name = "1000000000000000000_tts_queue_S_ab_s0.wav"
        self.drop(name)
        self.inbox.start()
        self.assertFalse((self.inbox_dir / name).exists())
        self.assertTrue((self.store_dir / name).exists())
        self.assertEqual(self.got[0].path, self.store_dir / name)

    def test_dot_names_are_ignored(self):
        self.drop(".1000000000000000000_tts_queue_S_ab_s0.wav.part")
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
        name = "1760000000123456789_tts_queue_sess-ion-42_0a1f_s17.wav"
        self.drop(name)
        self.inbox.start()
        self.assertEqual(self.got, [Sentence(
            recv_ns=1760000000123456789,
            session_id="sess-ion-42",
            token="0a1f",
            index=17,
            path=self.store_dir / name,
        )])


if __name__ == "__main__":
    unittest.main()
