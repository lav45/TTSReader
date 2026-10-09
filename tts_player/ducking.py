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

    def __init__(self, state_file: Path, own_pid: int, orphans_file: Path,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._state_file = state_file
        self._own_pid = own_pid
        self._orphans_file = orphans_file
        self._orphans = self._load_orphans()
        self._orphans_changed_at = clock() if self._orphans else None
        self._clock = clock
        self._first_seen: dict[tuple, float] = {}
        self._saved: dict[int, dict] = {}
        self._percent: int | None = None
        self._lowered_once = False
        self._stalled = False

    @property
    def ducked(self) -> bool:
        """Снижение запрошено (duck был, restore ещё нет); потоки могут быть ещё не тронуты."""
        return self._percent is not None

    @property
    def has_orphans(self) -> bool:
        """Есть приглушённые потоки, закончившиеся во время речи: громкость их приложения ещё надо исправить."""
        return bool(self._orphans)

    @property
    def orphans_changed_at(self) -> float | None:
        """Момент (по clock) последнего изменения сирот; None — сирот не было."""
        return self._orphans_changed_at

    def duck(self, percent: int) -> None:
        """Снизить чужие потоки до percent % (0–99). 100 — ничего.

        Ветка «уже приглушено → refresh» — защита от повторного вызова: duck() второй раз
        не перезаписывает запомненные громкости (они уже сниженные), а только дорабатывает
        новые потоки."""
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
        self._lower_adopting(foreign)

    def refresh(self) -> None:
        """Приглушить потоки, появившиеся после duck.

        После таймаута pactl или при отсутствии pactl — ничего, до следующего duck / restore
        (сервер завис — каждый вызов блокировал бы главный цикл на PACTL_TIMEOUT; нет
        программы — предупреждение каждую секунду бессмысленно). Код возврата != 0 —
        повтор остаётся. Если первый duck не получил список потоков — повторяет его.
        Запомненный поток, которого нет в списке, становится сиротой; новый поток
        приложения-сироты глушится сразу, от исходной громкости сироты."""
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
        self._orphan_gone(foreign)
        unsaved = {index: entry for index, entry in foreign.items() if not self._is_saved(index, entry)}
        matches = self._claim(unsaved)
        fresh = {}
        for index, entry in unsaved.items():
            first_seen = self._first_seen.setdefault(self._key(index, entry), now)
            if index in matches or now - first_seen >= self.MIN_AGE_S:
                fresh[index] = entry
        self._lower(fresh, matches)

    def watch(self) -> None:
        """Один проход по списку потоков: вне приглушения исправить громкость потоков сирот.

        При приглушении — refresh. Нет сирот или pactl завис / отсутствует — ничего."""
        if self.ducked:
            self.refresh()
            return
        if not self._orphans or self._stalled:
            return
        inputs = self._list_inputs()
        if inputs is None:
            return
        foreign = self._foreign(inputs)
        matches = self._claim(foreign)
        for index, orphan in matches.items():
            self._pactl("set-sink-input-volume", str(index), *map(str, orphan["original"]))
        self._consume(matches)

    def restore(self) -> None:
        """Вернуть запомненные громкости потокам, которые ещё есть; удалить state_file.

        Нечего возвращать — pactl не зовём. Не удалось получить список потоков —
        set пробуем вслепую для каждого запомненного."""
        saved, percent = self._saved, self._percent
        self._saved = {}
        self._percent = None
        self._lowered_once = False
        self._stalled = False
        self._first_seen = {}
        if saved:
            present = self._present()
            self._apply(saved, present)
            if present is not None and percent is not None:
                self._add_orphans(saved, present, percent)
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

    def _is_saved(self, index: int, entry: dict) -> bool:
        """Поток уже запомнен: тот же index, имя и pid."""
        saved = self._saved.get(index)
        return saved is not None and saved["name"] == entry["name"] and saved["pid"] == entry["pid"]

    def _orphan_gone(self, foreign: dict[int, dict]) -> None:
        """Запомненные потоки, которых нет в списке, — сироты; из запомненных они уходят."""
        present = {index: (entry["name"], entry["pid"]) for index, entry in foreign.items()}
        gone = {index: entry for index, entry in self._saved.items()
                if present.get(index) != (entry["name"], entry["pid"])}
        if not gone:
            return
        for index in gone:
            del self._saved[index]
        self._write_state()
        self._add_orphans(gone, present, self._percent)

    def _add_orphans(self, saved: dict[int, dict], present: dict, percent: int) -> None:
        """Сироты из saved — запомненных потоков, которых (с тем же именем и pid) нет среди present.

        known — потоки приложения, звучащие в этот момент: сирота их не трогает."""
        added = False
        for index, entry in saved.items():
            name = entry["name"]
            if present.get(index) == (name, entry["pid"]) or name is None:
                continue
            known = [[i, pid] for i, (n, pid) in present.items() if n == name]
            ducked = [round(v * percent / 100) for v in entry["volumes"]]
            self._orphans[name] = {"original": entry["volumes"], "ducked": ducked, "known": known}
            added = True
        if added:
            self._write_orphans()

    def _claim(self, streams: dict[int, dict]) -> dict[int, dict]:
        """Потоки, которым достаётся сирота: {index: сирота}. Сироты не снимаются.

        Поток приложения-сироты «новый», если его нет среди known сироты. Среди новых
        подходящий — с громкостью == ducked ±1 по каналам; нашёлся — он получает сироту,
        новые есть, а подходящего нет — сирота снимается (громкость другая — поправил
        пользователь, либо число каналов другое). Уже звучавшие потоки сироту не трогают."""
        matches = {}
        dropped = False
        for name, orphan in list(self._orphans.items()):
            known = {tuple(item) for item in orphan["known"]}
            new = {index: entry for index, entry in streams.items()
                   if entry["name"] == name and (index, entry["pid"]) not in known}
            if not new:
                continue
            index = next((i for i, e in new.items() if self._is_close(e["volumes"], orphan["ducked"])), None)
            if index is None:
                log.info("Громкость %s другая (или другое число каналов), сирота снята", name)
                del self._orphans[name]
                dropped = True
            else:
                matches[index] = orphan
        if dropped:
            self._write_orphans()
        return matches

    def _consume(self, matches: dict[int, dict]) -> None:
        """Снять сирот, выданных _claim, после того как громкость выставлена."""
        for orphan in matches.values():
            for name in [n for n, o in self._orphans.items() if o is orphan]:
                del self._orphans[name]
        if matches:
            self._write_orphans()

    def _lower_adopting(self, streams: dict[int, dict]) -> None:
        """Снизить streams; новым потокам приложений-сирот исходная громкость — из сироты."""
        matches = self._claim(streams)
        self._lower(streams, matches)

    @staticmethod
    def _is_close(current: list[int], ducked: list[int]) -> bool:
        return len(current) == len(ducked) and all(abs(a - b) <= 1 for a, b in zip(current, ducked))

    def _load_orphans(self) -> dict[str, dict]:
        if not self._orphans_file.exists():
            return {}
        try:
            data = json.loads(self._orphans_file.read_text(encoding="utf-8"))
            return {
                str(name): {
                    "original": [int(v) for v in entry["original"]],
                    "ducked": [int(v) for v in entry["ducked"]],
                    "known": [[int(i), pid] for i, pid in entry["known"]],
                }
                for name, entry in data.items()
            }
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("Файл сирот %s повреждён, удалён: %s", self._orphans_file, exc)
            try:
                self._orphans_file.unlink(missing_ok=True)
            except OSError as error:
                log.warning("Не удалён %s: %s", self._orphans_file, error)
            return {}

    def _write_orphans(self) -> None:
        self._orphans_changed_at = self._clock()
        try:
            if self._orphans:
                write_json(self._orphans_file, self._orphans)
            else:
                self._orphans_file.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Не записан %s: %s", self._orphans_file, exc)

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

    def _lower(self, streams: dict[int, dict], matches: dict[int, dict] | None = None) -> None:
        """Запомнить потоки в state_file и снизить их на self._percent.

        У потоков из matches (сироты) исходная громкость — original сироты; после записи
        состояния и снижения сироты снимаются."""
        if not streams:
            return
        matches = matches or {}
        streams = {index: ({**entry, "volumes": matches[index]["original"]} if index in matches else entry)
                   for index, entry in streams.items()}
        saved_before = dict(self._saved)
        self._saved.update(streams)
        if not self._write_state():
            self._saved = saved_before  # без записи recover не вернёт громкость — не снижаем
            return
        for index, entry in streams.items():
            values = [round(v * self._percent / 100) for v in entry["volumes"]]
            self._pactl("set-sink-input-volume", str(index), *map(str, values))
        self._consume({index: orphan for index, orphan in matches.items() if index in streams})

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
        except FileNotFoundError as exc:
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

    def _write_state(self) -> bool:
        data = {"inputs": {str(index): entry for index, entry in self._saved.items()}}
        try:
            write_json(self._state_file, data)
        except OSError as exc:
            log.warning("Не записан %s: %s", self._state_file, exc)
            return False
        return True

    def _remove_state(self) -> None:
        try:
            self._state_file.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Не удалён %s: %s", self._state_file, exc)
