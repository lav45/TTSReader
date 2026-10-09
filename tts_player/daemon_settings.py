"""Настройки claude-tts, которые меняет плеер: голос (персона) и скорость.

Читает и пишет <root>/config.json и <root>/sessions.d/*.json (root = ~/.claude-tts)
в формате демона; чужие ключи не трогает. Файлы пишутся атомарно (временный + rename).
"""
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from tts_player import atomic_json

log = logging.getLogger("tts_player.daemon_settings")

CONFIG_NAME = "config.json"
SESSIONS_DIR = "sessions.d"
SESSION_KEYS = ("persona", "speed")
DEFAULT_SPEED = 2.0  # как в claude-tts get_persona_config, если у персоны нет speed


@dataclass(frozen=True)
class Voice:
    name: str
    description: str  # "" если у персоны нет описания


@dataclass(frozen=True)
class VoiceSettings:
    voices: list[Voice]
    active: str
    speed: float


class SettingsError(Exception):
    """Причина, по которой настройки не прочитаны или не сохранены (текст для экрана)."""


_FileGone = atomic_json.FileGone  # файл сессии удалён демоном между чтением и записью


class DaemonSettings:
    SPEED_MIN = 0.5
    SPEED_MAX = 4.0

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._config = self._root / CONFIG_NAME
        self._sessions = self._root / SESSIONS_DIR

    def load(self) -> VoiceSettings:
        config = self._read_config()
        personas = config["personas"]
        active = config.get("active_persona")
        if not isinstance(active, str) or active not in personas:
            raise SettingsError(
                f"В {self._config} активная персона {active!r} не найдена среди personas"
            )
        voices = [
            Voice(name, str(persona.get("description") or ""))
            for name, persona in personas.items()
        ]
        speed = personas[active].get("speed", DEFAULT_SPEED)
        if self._not_number(speed):
            raise SettingsError(f"В {self._config} у персоны {active!r} скорость не число")
        if not self.SPEED_MIN <= speed <= self.SPEED_MAX:
            # спин прижал бы её к границе, и «Сохранить» молча записал бы другую скорость
            raise SettingsError(
                f"В {self._config} у персоны {active!r} скорость {speed}"
                f" вне диапазона {self.SPEED_MIN}–{self.SPEED_MAX}"
            )
        return VoiceSettings(voices=voices, active=active, speed=float(speed))

    def save(self, persona: str, speed: float) -> None:
        config = self._read_config()
        if persona not in config["personas"]:
            raise SettingsError(f"Персоны {persona!r} нет в {self._config}")
        if self._not_number(speed) or not self.SPEED_MIN <= speed <= self.SPEED_MAX:
            raise SettingsError(
                f"Скорость {speed!r} вне диапазона {self.SPEED_MIN}–{self.SPEED_MAX}"
            )
        self._remove_leftovers()
        config["active_persona"] = persona
        for entry in config["personas"].values():
            entry["speed"] = speed
        config.pop("project_personas", None)
        self._write_json(self._config, config)
        self._clean_sessions()

    @staticmethod
    def _not_number(value) -> bool:
        return isinstance(value, bool) or not isinstance(value, (int, float))

    def _remove_leftovers(self) -> None:
        """Временные файлы прошлого сбойного save() (шаблон mkstemp из _write_json).

        Чужие config.tmp и <sid>.tmp демона не совпадают с шаблоном и не трогаются."""
        leftovers = [
            *self._root.glob(CONFIG_NAME + ".*.tmp"),
            *self._sessions.glob("*.json.*.tmp"),
        ]
        for path in leftovers:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.debug("Не удалён остаток %s", path)

    def _read_config(self) -> dict:
        try:
            text = self._config.read_text(encoding="utf-8")
        except OSError as e:
            raise SettingsError(f"Не удалось прочитать {self._config}: {e.strerror or e}") from e
        try:
            config = json.loads(text)
        except ValueError as e:
            raise SettingsError(f"{self._config} не является корректным JSON: {e}") from e
        if not isinstance(config, dict):
            raise SettingsError(f"{self._config}: ожидался JSON-объект")
        personas = config.get("personas")
        if not isinstance(personas, dict) or not personas:
            raise SettingsError(f"В {self._config} нет списка персон personas")
        if not all(isinstance(p, dict) for p in personas.values()):
            raise SettingsError(f"В {self._config} персона описана не объектом")
        return config

    def _clean_sessions(self) -> None:
        # Сессионные переопределения иначе перекрыли бы выбор плеера.
        if not self._sessions.is_dir():
            return
        for path in sorted(self._sessions.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.debug("Пропущен файл сессии %s", path)
                continue
            if not isinstance(data, dict) or not any(k in data for k in SESSION_KEYS):
                continue
            for key in SESSION_KEYS:
                data.pop(key, None)
            try:
                self._write_json(path, data, create=False)
            except _FileGone:
                log.debug("Файл сессии %s удалён демоном, пропущен", path)

    @staticmethod
    def _write_json(path: Path, data: dict, create: bool = True) -> None:
        """Атомарная запись (tts_player.atomic_json); OSError -> SettingsError.

        create=False: файла уже нет (удалён демоном) — _FileGone, не воскрешаем."""
        try:
            atomic_json.write_json(path, data, create=create)
        except OSError as e:
            raise SettingsError(f"Не удалось записать {path}: {e.strerror or e}") from e
