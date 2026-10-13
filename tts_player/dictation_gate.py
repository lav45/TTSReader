"""Ворота диктовки: «звука сейчас нельзя», пока Claude Code слушает диктовку, и ещё gap_ms
после неё. Один DictationGate на процесс: DictationWatch сообщает диктовку, Player и
TextSpeaker подписываются на смену ворот."""
import logging
from typing import Callable

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

log = logging.getLogger("tts_player.dictation_gate")


class DictationGate:
    """closed() — диктовка идёт или отсчёт gap_ms после неё не истёк.

    set_dictating(active) зовёт DictationWatch из цикла GLib. Подписчики зовутся из цикла GLib
    только при смене closed(): диктовка во время отсчёта его снимает молча (ворота и так
    закрыты), следующий конец диктовки начинает отсчёт заново. gap_ms, заданный новым
    значением, действует с ближайшего отсчёта, идущий не трогается. Ошибка подписчика
    пишется в журнал и остальных не останавливает. Подписчик не зовёт set_dictating: вложенный
    вызов дошёл бы до следующих подписчиков раньше, чем внешний, — в обратном порядке. stop() —
    выход плеера: таймер снят, колбэков больше нет, и тем, до кого текущий обход не дошёл, тоже."""

    def __init__(self, gap_ms: int = 3000) -> None:
        self.gap_ms = gap_ms
        self._dictating = False
        self._timer: int | None = None  # идущий отсчёт после диктовки
        self._stopped = False
        self._subscribers: list[Callable[[], None]] = []

    def closed(self) -> bool:
        return self._dictating or self._timer is not None

    def subscribe(self, on_change: Callable[[], None]) -> None:
        self._subscribers.append(on_change)

    def set_dictating(self, active: bool) -> None:
        if self._stopped or active == self._dictating:
            return
        self._dictating = active
        if active:
            was_closed = self._timer is not None
            self._cancel_timer()
            if not was_closed:
                self._notify()
            return
        if self.gap_ms <= 0:
            self._notify()
            return
        self._timer = GLib.timeout_add(self.gap_ms, self._gap_elapsed)

    def stop(self) -> None:
        self._stopped = True
        self._cancel_timer()

    def _gap_elapsed(self) -> bool:
        self._timer = None
        self._notify()
        return GLib.SOURCE_REMOVE

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None

    def _notify(self) -> None:
        for on_change in list(self._subscribers):
            if self._stopped:  # подписчик остановил ворота — остальным не звать
                return
            try:
                on_change()
            except Exception:
                log.exception("on_change: ошибка подписчика ворот диктовки")
