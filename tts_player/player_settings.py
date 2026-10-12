"""Собственные настройки плеера: сколько последних реплик озвучивать (tail),
автовоспроизведение (autoplay), пауза других проигрывателей на время речи (pause_others) и
тишина до и после речи, пока они стоят (gap_ms).

Файл $XDG_CONFIG_HOME/tts-player/settings.json (иначе ~/.config/tts-player/settings.json):
{"tail": <целое 1-20>, "autoplay": <true|false>, "pause_others": <true|false>,
 "gap_ms": <целое 0-10000>}.
Лишние ключи игнорируются. Чтение не бросает:
на любой порче возвращаются значения по умолчанию и пишется предупреждение в лог (stderr).
"""
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tts_player import atomic_json
from tts_player.settings_error import SettingsError

log = logging.getLogger(__name__)

APP_DIR = "tts-player"
FILE_NAME = "settings.json"


@dataclass(frozen=True)
class Prefs:
    tail: int = 5
    autoplay: bool = True
    pause_others: bool = True
    gap_ms: int = 3000


class PlayerSettings:
    TAIL_MIN = 1
    TAIL_MAX = 20
    GAP_MIN_MS = 0
    GAP_MAX_MS = 10000

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @staticmethod
    def default_path(env: Mapping[str, str]) -> Path:
        """XDG_CONFIG_HOME, если задан и не пуст, иначе HOME/.config (HOME из env)."""
        base = env.get("XDG_CONFIG_HOME")
        if base:
            return Path(base) / APP_DIR / FILE_NAME
        home = env.get("HOME")
        root = Path(home) if home else Path.home()
        return root / ".config" / APP_DIR / FILE_NAME

    def load(self) -> Prefs:
        defaults = Prefs()
        data = self._read_object()
        if data is None:
            return defaults
        tail = data.get("tail", defaults.tail)
        if not self._tail_valid(tail):
            log.warning("%s: tail %r вне %d-%d или не целое, взято %d",
                        self._path, tail, self.TAIL_MIN, self.TAIL_MAX, defaults.tail)
            tail = defaults.tail
        autoplay = data.get("autoplay", defaults.autoplay)
        if not isinstance(autoplay, bool):  # 0/1 — тоже не bool
            log.warning("%s: autoplay %r не true/false, взято %s",
                        self._path, autoplay, str(defaults.autoplay).lower())
            autoplay = defaults.autoplay
        pause_others = data.get("pause_others", defaults.pause_others)
        if not isinstance(pause_others, bool):
            log.warning("%s: pause_others %r не true/false, взято %s",
                        self._path, pause_others, str(defaults.pause_others).lower())
            pause_others = defaults.pause_others
        gap_ms = data.get("gap_ms", defaults.gap_ms)
        if not self._gap_valid(gap_ms):
            log.warning("%s: gap_ms %r вне %d-%d или не целое, взято %d",
                        self._path, gap_ms, self.GAP_MIN_MS, self.GAP_MAX_MS, defaults.gap_ms)
            gap_ms = defaults.gap_ms
        return Prefs(tail=tail, autoplay=autoplay, pause_others=pause_others, gap_ms=gap_ms)

    def save(self, prefs: Prefs) -> None:
        if not self._tail_valid(prefs.tail):
            raise SettingsError(
                f"tail {prefs.tail!r} вне диапазона {self.TAIL_MIN}-{self.TAIL_MAX}"
            )
        if not self._gap_valid(prefs.gap_ms):
            raise SettingsError(
                f"gap_ms {prefs.gap_ms!r} вне диапазона {self.GAP_MIN_MS}-{self.GAP_MAX_MS}"
            )
        data = {"tail": prefs.tail, "autoplay": prefs.autoplay,
                "pause_others": prefs.pause_others, "gap_ms": prefs.gap_ms}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json.write_json(self._path, data)
        except OSError as e:
            raise SettingsError(f"Не удалось записать {self._path}: {e.strerror or e}") from e

    def _read_object(self) -> dict | None:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            log.warning("Не удалось прочитать %s: %s", self._path, e.strerror or e)
            return None
        try:
            data = json.loads(text)
        except ValueError as e:
            log.warning("%s не является корректным JSON: %s", self._path, e)
            return None
        if not isinstance(data, dict):
            log.warning("%s: ожидался JSON-объект", self._path)
            return None
        return data

    @classmethod
    def _tail_valid(cls, value) -> bool:
        return cls._int_within(value, cls.TAIL_MIN, cls.TAIL_MAX)

    @classmethod
    def _gap_valid(cls, value) -> bool:
        return cls._int_within(value, cls.GAP_MIN_MS, cls.GAP_MAX_MS)

    @staticmethod
    def _int_within(value, low: int, high: int) -> bool:
        """Целое (bool — не целое) в пределах [low, high]."""
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        return low <= value <= high
