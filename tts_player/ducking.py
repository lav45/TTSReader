"""Приглушение чужих аудиопотоков через pactl на время озвучки.

Ducker запоминает громкости чужих sink-input'ов в state_file (атомарно, чтобы
после падения плеера их вернул recover), снижает их и возвращает обратно.
Ошибки pactl (нет программы, код != 0, таймаут, не JSON) — предупреждение в лог,
наружу исключение не выходит."""
import json
import logging
import os
import subprocess
from pathlib import Path

from tts_player.atomic_json import write_json

log = logging.getLogger(__name__)

PACTL_TIMEOUT = 2
PID_PROPERTY = "application.process.id"


class Ducker:
    """Снижает и возвращает громкость чужих потоков (все, кроме процесса own_pid)."""

    def __init__(self, state_file: Path, own_pid: int) -> None:
        self._state_file = state_file
        self._own_pid = own_pid
        self._saved: dict[int, list[int]] = {}
        self._percent: int | None = None

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
        inputs = self._list_inputs()
        if inputs is None:
            return
        self._percent = percent
        self._lower(self._foreign(inputs))

    def refresh(self) -> None:
        """Приглушить потоки, появившиеся после duck."""
        if not self.ducked:
            return
        inputs = self._list_inputs()
        if inputs is None:
            return
        fresh = {index: values for index, values in self._foreign(inputs).items() if index not in self._saved}
        self._lower(fresh)

    def restore(self) -> None:
        """Вернуть запомненные громкости потокам, которые ещё есть; удалить state_file."""
        inputs = self._list_inputs()
        present = None if inputs is None else {item["index"] for item in inputs}
        self._apply(self._saved, present)
        self._saved = {}
        self._percent = None
        self._remove_state()

    def recover(self) -> None:
        """При старте: вернуть громкости из оставленного state_file и удалить его."""
        if not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            saved = {int(index): [int(v) for v in values] for index, values in data["inputs"].items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("Файл приглушения %s повреждён, удалён: %s", self._state_file, exc)
            self._remove_state()
            return
        inputs = self._list_inputs()
        present = None if inputs is None else {item["index"] for item in inputs}
        self._apply(saved, present)
        self._remove_state()

    def _foreign(self, inputs: list[dict]) -> dict[int, list[int]]:
        """Громкости по каналам у потоков, чей процесс не наш."""
        result = {}
        for item in inputs:
            try:
                if item["properties"].get(PID_PROPERTY) == str(self._own_pid):
                    continue
                result[item["index"]] = [channel["value"] for channel in item["volume"].values()]
            except (KeyError, TypeError, AttributeError):
                log.warning("Неожиданная запись sink-input: %r", item)
        return result

    def _lower(self, streams: dict[int, list[int]]) -> None:
        """Запомнить потоки в state_file и снизить их на self._percent."""
        if not streams:
            return
        self._saved.update(streams)
        self._write_state()
        lowered = {index: [round(v * self._percent / 100) for v in values] for index, values in streams.items()}
        self._apply(lowered, None)

    def _apply(self, volumes: dict[int, list[int]], present: set[int] | None) -> None:
        """set-sink-input-volume для потоков из present (None — для всех)."""
        for index, values in volumes.items():
            if present is not None and index not in present:
                continue
            self._pactl("set-sink-input-volume", str(index), *map(str, values))

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
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("pactl %s: %s", " ".join(args), exc)
            return None
        if proc.returncode != 0:
            log.warning("pactl %s: код %d: %s", " ".join(args), proc.returncode, proc.stderr.strip())
            return None
        return proc.stdout

    def _write_state(self) -> None:
        data = {"inputs": {str(index): values for index, values in self._saved.items()}}
        try:
            write_json(self._state_file, data)
        except OSError as exc:
            log.warning("Не записан %s: %s", self._state_file, exc)

    def _remove_state(self) -> None:
        try:
            self._state_file.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Не удалён %s: %s", self._state_file, exc)
