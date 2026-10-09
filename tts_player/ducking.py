"""Приглушение чужих аудиопотоков через pactl на время озвучки.

Ducker запоминает громкости чужих sink-input'ов в state_file (атомарно, чтобы
после падения плеера их вернул recover), снижает их и возвращает обратно.
Ошибки pactl (нет программы, код != 0, таймаут, не JSON) — предупреждение в лог,
наружу исключение не выходит."""
import json
import logging
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from tts_player.atomic_json import write_json

log = logging.getLogger(__name__)

PACTL_TIMEOUT = 2
PID_PROPERTY = "application.process.id"


class Ducker:
    """Снижает и возвращает громкость чужих потоков (все, кроме процесса own_pid)."""

    MIN_AGE_S = 5.0

    def __init__(self, state_file: Path, own_pid: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._state_file = state_file
        self._own_pid = own_pid
        self._clock = clock
        self._first_seen: dict[tuple, float] = {}
        self._saved: dict[int, dict] = {}
        self._percent: int | None = None
        self._lowered_once = False
        self._stalled = False

    @property
    def ducked(self) -> bool:
        """Громкость сейчас снижена (duck был, restore ещё нет)."""
        return self._percent is not None

    def duck(self, percent: int) -> None:
        """Снизить чужие потоки до percent % (0–99). 100 — ничего; повторный вызов — refresh."""
        if percent >= 100:
            return
        if self.ducked:
            self.refresh()
            return
        self._percent = percent
        self._stalled = False
        self._duck_initial()

    def _duck_initial(self) -> None:
        """Первый список потоков после duck: все они «старые» и глушатся сразу."""
        inputs = self._list_inputs()
        if inputs is None:
            return
        self._lowered_once = True
        foreign = self._foreign(inputs)
        for index, entry in foreign.items():
            self._first_seen[self._key(index, entry)] = float("-inf")
        self._lower(foreign)

    def refresh(self) -> None:
        """Приглушить потоки, появившиеся после duck.

        После таймаута pactl — ничего, до следующего duck / restore (звуковой сервер завис,
        каждый вызов блокировал бы главный цикл на PACTL_TIMEOUT). Если первый duck не
        получил список потоков — повторяет его."""
        if not self.ducked or self._stalled:
            return
        if not self._lowered_once:
            self._duck_initial()
            return
        inputs = self._list_inputs()
        if inputs is None:
            return
        now = self._clock()
        foreign = self._foreign(inputs)
        keys = {self._key(index, entry) for index, entry in foreign.items()}
        self._first_seen = {key: seen for key, seen in self._first_seen.items() if key in keys}
        fresh = {}
        for index, entry in foreign.items():
            saved = self._saved.get(index)
            if saved is not None and saved["name"] == entry["name"] and saved["pid"] == entry["pid"]:
                continue
            first_seen = self._first_seen.setdefault(self._key(index, entry), now)
            if now - first_seen >= self.MIN_AGE_S:
                fresh[index] = entry
        self._lower(fresh)

    def restore(self) -> None:
        """Вернуть запомненные громкости потокам, которые ещё есть; удалить state_file.

        Нечего возвращать — pactl не зовём. Не удалось получить список потоков —
        set пробуем вслепую для каждого запомненного."""
        saved = self._saved
        self._saved = {}
        self._percent = None
        self._lowered_once = False
        self._stalled = False
        self._first_seen = {}
        if saved:
            self._apply(saved, self._present())
        self._remove_state()

    def recover(self) -> None:
        """При старте: вернуть громкости из оставленного state_file и удалить его."""
        if not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            saved = {
                int(index): {
                    "volumes": [int(v) for v in entry["volumes"]],
                    "name": entry["name"],
                    "pid": entry["pid"],
                }
                for index, entry in data["inputs"].items()
            }
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("Файл приглушения %s повреждён, удалён: %s", self._state_file, exc)
            self._remove_state()
            return
        if saved:
            self._apply(saved, self._present())
        self._remove_state()

    @staticmethod
    def _is_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    def _identity(self, item: dict) -> tuple[int, str | None, str | None]:
        """(index, имя приложения, pid) потока; KeyError/TypeError/AttributeError — запись негодна."""
        properties = item["properties"]
        index = item["index"]
        name = properties.get("application.name")
        pid = properties.get(PID_PROPERTY)
        if not self._is_int(index) or not all(v is None or isinstance(v, str) for v in (name, pid)):
            raise TypeError("негодные index / имя / pid")
        return index, name, pid

    def _present(self) -> dict[int, tuple[str | None, str | None]] | None:
        """{index: (имя, pid)} живых потоков; None — список получить не удалось."""
        inputs = self._list_inputs()
        if inputs is None:
            return None
        result = {}
        for item in inputs:
            try:
                index, name, pid = self._identity(item)
            except (KeyError, TypeError, AttributeError):
                log.warning("Неожиданная запись sink-input: %r", item)
                continue
            result[index] = (name, pid)
        return result

    def _foreign(self, inputs: list[dict]) -> dict[int, dict]:
        """Запись {volumes, name, pid} для потоков, чей процесс не наш."""
        result = {}
        for item in inputs:
            try:
                index, name, pid = self._identity(item)
                if pid == str(self._own_pid):
                    continue
                volumes = [channel["value"] for channel in item["volume"].values()]
                if not volumes or not all(self._is_int(v) for v in volumes):
                    raise TypeError("негодная громкость")
            except (KeyError, TypeError, AttributeError):
                log.warning("Неожиданная запись sink-input: %r", item)
                continue
            result[index] = {"volumes": volumes, "name": name, "pid": pid}
        return result

    @staticmethod
    def _key(index: int, entry: dict) -> tuple:
        """Идентичность потока для учёта возраста: index + имя + pid."""
        return index, entry["name"], entry["pid"]

    def _lower(self, streams: dict[int, dict]) -> None:
        """Запомнить потоки в state_file и снизить их на self._percent."""
        if not streams:
            return
        self._saved.update(streams)
        self._write_state()
        for index, entry in streams.items():
            values = [round(v * self._percent / 100) for v in entry["volumes"]]
            self._pactl("set-sink-input-volume", str(index), *map(str, values))

    def _apply(self, saved: dict[int, dict], present: dict | None) -> None:
        """Вернуть громкости: только потокам с тем же index, именем и pid (present None — всем)."""
        for index, entry in saved.items():
            if present is not None:
                if index not in present:
                    continue
                if present[index] != (entry["name"], entry["pid"]):
                    log.info("Поток %d стал другим приложением, громкость не возвращена", index)
                    continue
            self._pactl("set-sink-input-volume", str(index), *map(str, entry["volumes"]))

    def _list_inputs(self) -> list[dict] | None:
        out = self._pactl("-f", "json", "list", "sink-inputs")
        if out is None:
            return None
        try:
            inputs = json.loads(out)
            if not isinstance(inputs, list):
                raise ValueError("ожидался массив")
        except ValueError as exc:
            log.warning("pactl вернул не JSON: %s", exc)
            return None
        return inputs

    def _pactl(self, *args: str) -> str | None:
        env = {**os.environ, "LC_ALL": "C"}
        try:
            proc = subprocess.run(["pactl", *args], capture_output=True, text=True,
                                  timeout=PACTL_TIMEOUT, env=env)
        except subprocess.TimeoutExpired as exc:
            self._stalled = True
            log.warning("pactl %s: %s", " ".join(args), exc)
            return None
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("pactl %s: %s", " ".join(args), exc)
            return None
        if proc.returncode != 0:
            log.warning("pactl %s: код %d: %s", " ".join(args), proc.returncode, proc.stderr.strip())
            return None
        return proc.stdout

    def _write_state(self) -> None:
        data = {"inputs": {str(index): entry for index, entry in self._saved.items()}}
        try:
            write_json(self._state_file, data)
        except OSError as exc:
            log.warning("Не записан %s: %s", self._state_file, exc)

    def _remove_state(self) -> None:
        try:
            self._state_file.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Не удалён %s: %s", self._state_file, exc)
