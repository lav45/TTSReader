import tests  # noqa: F401 — окружение тестов (tests/__init__.py)
import unittest

from tts_player.output_idle import SUSPEND_AFTER_S, OutputIdle


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class OutputIdleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.idle = OutputIdle(self.clock)

    def test_never_sounded_is_asleep(self) -> None:
        self.assertTrue(self.idle.asleep())

    def test_sounding_is_awake(self) -> None:
        self.idle.begin()
        self.clock.now += SUSPEND_AFTER_S * 3
        self.assertFalse(self.idle.asleep())

    def test_awake_right_after_end(self) -> None:
        self.idle.begin()
        self.idle.end()
        self.clock.now += SUSPEND_AFTER_S - 0.1
        self.assertFalse(self.idle.asleep())

    def test_asleep_after_suspend_timeout(self) -> None:
        self.idle.begin()
        self.idle.end()
        self.clock.now += SUSPEND_AFTER_S + 0.1
        self.assertTrue(self.idle.asleep())

    def test_one_of_two_still_sounding_keeps_awake(self) -> None:
        self.idle.begin()
        self.idle.begin()
        self.idle.end()
        self.clock.now += SUSPEND_AFTER_S * 3
        self.assertFalse(self.idle.asleep())


if __name__ == "__main__":
    unittest.main()
