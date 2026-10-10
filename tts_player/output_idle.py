"""Простой вывода звука плеера: спит ли устройство к следующему запуску.

WirePlumber усыпляет устройство вывода через session.suspend-timeout-seconds (по умолчанию
5 с) без потоков, и проснувшийся Bluetooth-sink теряет начало звука. Один OutputIdle на
процесс делят все Playback (очередь и свой текст): звук любого из них будит вывод для всех.
"""
import time
from typing import Callable

SUSPEND_AFTER_S = 5.0  # session.suspend-timeout-seconds WirePlumber по умолчанию


class OutputIdle:
    """begin() — конвейер начал звучать, end() — перестал; asleep() — вывод молчит дольше
    SUSPEND_AFTER_S или не звучал ни разу с запуска."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._sounding = 0
        self._ended_at: float | None = None

    def begin(self) -> None:
        self._sounding += 1

    def end(self) -> None:
        self._sounding -= 1
        self._ended_at = self._clock()

    def asleep(self) -> bool:
        if self._sounding > 0:
            return False
        if self._ended_at is None:
            return True
        return self._clock() - self._ended_at > SUSPEND_AFTER_S
