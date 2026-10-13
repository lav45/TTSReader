"""Тесты DictationGate: время — главный цикл GLib с малым gap_ms."""
import tests  # noqa: F401 — окружение тестов (tests/__init__.py)
import unittest

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from tts_player.dictation_gate import DictationGate  # noqa: E402

GAP_MS = 60


def spin(milliseconds: int) -> None:
    """Главный цикл GLib работает milliseconds, затем возвращается."""
    loop = GLib.MainLoop()
    GLib.timeout_add(milliseconds, loop.quit)
    loop.run()


class DictationGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[bool] = []
        self.gate = DictationGate(gap_ms=GAP_MS)
        self.addCleanup(self.gate.stop)
        self.gate.subscribe(lambda: self.calls.append(self.gate.closed()))

    def test_open_at_start(self) -> None:
        self.assertFalse(self.gate.closed())

    def test_dictation_closes_once(self) -> None:
        self.gate.set_dictating(True)
        self.assertTrue(self.gate.closed())
        self.assertEqual([True], self.calls)

    def test_end_opens_after_gap(self) -> None:
        self.gate.set_dictating(True)
        self.gate.set_dictating(False)
        self.assertTrue(self.gate.closed())
        spin(GAP_MS // 2)
        self.assertTrue(self.gate.closed())
        self.assertEqual([True], self.calls)
        spin(GAP_MS)
        self.assertFalse(self.gate.closed())
        self.assertEqual([True, False], self.calls)

    def test_dictation_during_gap_cancels_countdown(self) -> None:
        self.gate.set_dictating(True)
        self.gate.set_dictating(False)
        spin(GAP_MS // 2)
        self.gate.set_dictating(True)
        self.assertEqual([True], self.calls)
        spin(GAP_MS)  # старый отсчёт истёк бы здесь
        self.assertTrue(self.gate.closed())
        self.assertEqual([True], self.calls)

    def test_next_end_counts_full_gap(self) -> None:
        self.gate.set_dictating(True)
        self.gate.set_dictating(False)
        spin(GAP_MS * 2 // 3)
        self.gate.set_dictating(True)
        self.gate.set_dictating(False)
        spin(GAP_MS * 2 // 3)  # со старого отсчёта прошло бы больше gap_ms
        self.assertTrue(self.gate.closed())
        spin(GAP_MS)
        self.assertFalse(self.gate.closed())
        self.assertEqual([True, False], self.calls)

    def test_zero_gap_opens_immediately(self) -> None:
        self.gate.gap_ms = 0
        self.gate.set_dictating(True)
        self.gate.set_dictating(False)
        self.assertFalse(self.gate.closed())
        self.assertEqual([True, False], self.calls)

    def test_new_gap_applies_to_next_countdown_only(self) -> None:
        self.gate.set_dictating(True)
        self.gate.set_dictating(False)
        self.gate.gap_ms = 10_000
        spin(GAP_MS * 2)
        self.assertFalse(self.gate.closed())

    def test_same_value_changes_nothing(self) -> None:
        self.gate.set_dictating(False)
        self.assertEqual([], self.calls)
        self.gate.set_dictating(True)
        self.gate.set_dictating(True)
        self.assertEqual([True], self.calls)
        self.gate.set_dictating(False)
        self.gate.set_dictating(False)
        spin(GAP_MS * 2)
        self.assertEqual([True, False], self.calls)

    def test_stop_cancels_timer_and_callbacks(self) -> None:
        self.gate.set_dictating(True)
        self.gate.set_dictating(False)
        self.gate.stop()
        spin(GAP_MS * 2)
        self.assertEqual([True], self.calls)

    def test_subscriber_error_does_not_break_gate(self) -> None:
        gate = DictationGate(gap_ms=0)
        self.addCleanup(gate.stop)
        seen: list[bool] = []

        def broken() -> None:
            raise RuntimeError("сбой подписчика")

        gate.subscribe(broken)
        gate.subscribe(lambda: seen.append(gate.closed()))
        with self.assertLogs("tts_player.dictation_gate", "ERROR"):
            gate.set_dictating(True)
        self.assertEqual([True], seen)


if __name__ == "__main__":
    unittest.main()
