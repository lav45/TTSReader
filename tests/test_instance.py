"""Блокировка единственности и команда stop (tts_player.instance) на временных каталогах."""
import multiprocessing
import os
import signal
import subprocess
import threading
import time
import tempfile
import unittest
from pathlib import Path

from tts_player.instance import Instance, send_stop


def hold_lock(runtime: str, ready, release) -> None:
    instance = Instance.acquire(Path(runtime))
    ready.put(instance is not None)
    release.get()


class InstanceTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runtime = Path(tmp.name)

    def test_acquire_writes_pid(self) -> None:
        import os
        instance = Instance.acquire(self.runtime)
        self.assertIsNotNone(instance)
        text = (self.runtime / "tts-player" / "player.lock").read_text()
        self.assertEqual(str(os.getpid()), text.strip())
        instance.release()

    def test_second_acquire_in_other_process_is_refused_with_pid(self) -> None:
        import os
        ctx = multiprocessing.get_context("fork")
        ready, release = ctx.Queue(), ctx.Queue()
        child = ctx.Process(target=hold_lock, args=(str(self.runtime), ready, release))
        child.start()
        self.addCleanup(child.join)
        self.addCleanup(release.put, None)
        self.assertTrue(ready.get(timeout=10))
        self.assertIsNone(Instance.acquire(self.runtime))
        self.assertEqual(child.pid, Instance.holder_pid(self.runtime))
        self.assertNotEqual(os.getpid(), child.pid)

    def test_lock_is_free_after_release(self) -> None:
        Instance.acquire(self.runtime).release()
        again = Instance.acquire(self.runtime)
        self.assertIsNotNone(again)
        again.release()

    def test_send_stop_with_free_lock_returns_false(self) -> None:
        self.assertFalse(send_stop(self.runtime))
        Instance.acquire(self.runtime).release()
        self.assertFalse(send_stop(self.runtime))

    def test_send_stop_signals_holder(self) -> None:
        received = []
        previous = signal.signal(signal.SIGUSR1, lambda *_: received.append(1))
        self.addCleanup(signal.signal, signal.SIGUSR1, previous)
        instance = Instance.acquire(self.runtime)
        self.addCleanup(instance.release)
        self.assertTrue(send_stop(self.runtime, marker=""))
        self.assertEqual([1], received)

    def test_probe_of_free_lock_does_not_touch_file(self) -> None:
        Instance.acquire(self.runtime).release()
        path = self.runtime / "tts-player" / "player.lock"
        path.write_text("999\n")
        self.assertFalse(send_stop(self.runtime))
        self.assertEqual("999\n", path.read_text())

    def test_stale_pid_of_foreign_process_is_not_killed(self) -> None:
        sleeper = subprocess.Popen(["sleep", "30"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        instance = Instance.acquire(self.runtime)
        self.addCleanup(instance.release)
        (self.runtime / "tts-player" / "player.lock").write_text(f"{sleeper.pid}\n")
        self.assertFalse(send_stop(self.runtime))
        self.assertIsNone(sleeper.poll())

    def test_empty_pid_is_reread_until_written(self) -> None:
        instance = Instance.acquire(self.runtime)
        self.addCleanup(instance.release)
        path = self.runtime / "tts-player" / "player.lock"
        path.write_text("")
        threading.Timer(0.3, path.write_text, args=(f"{os.getpid()}\n",)).start()
        started = time.monotonic()
        self.assertEqual(os.getpid(), Instance.holder_pid(self.runtime))
        self.assertGreaterEqual(time.monotonic() - started, 0.25)


if __name__ == "__main__":
    unittest.main()
