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
        matches = self._orphan_matches(foreign)
        originals = {index: self._orphans[name]["original"] for index, name in matches.items()}
        self._lower(foreign, originals, set(matches.values()))

    def refresh(self) -> None:
        """Приглушить потоки, появившиеся после duck.

        После таймаута pactl или при отсутствии pactl — ничего, до следующего duck / restore
        (сервер завис — каждый вызов блокировал бы главный цикл на PACTL_TIMEOUT; нет
        программы — предупреждение каждую секунду бессмысленно). Код возврата != 0 —
        повтор остаётся. Если первый duck не получил список потоков — повторяет его.
        Запомненный поток, которого нет в списке, а других запомненных потоков приложения
        нет, становится сиротой. Поток, появившийся после начала речи, с громкостью ==
        сниженной ±1 у запомненного потока того же приложения (или у сироты) глушится сразу,
        от исходной громкости этой пары."""
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
        before = self._before_speech()
        self._orphan_gone(foreign, before)
        unsaved = {index: entry for index, entry in foreign.items() if not self._is_saved(index, entry)}
        originals = self._live_matches(unsaved, before)
        rest = {index: entry for index, entry in unsaved.items() if index not in originals}
        matches = self._orphan_matches(rest)
        for index, name in matches.items():
            originals[index] = self._orphans[name]["original"]
        fresh = {}
        for index, entry in unsaved.items():
            first_seen = self._first_seen.setdefault(self._key(index, entry), now)
            if index in originals or now - first_seen >= self.MIN_AGE_S:
                fresh[index] = entry
        self._lower(fresh, originals, set(matches.values()))

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
        matches = self._orphan_matches(self._foreign(inputs))
        failed = set()
        for index, name in matches.items():
            original = self._orphans[name]["original"]
            if self._pactl("set-sink-input-volume", str(index), *map(str, original)) is None:
                failed.add(name)
        self._consume(set(matches.values()) - failed)

    def restore(self) -> None:
        """Вернуть запомненные громкости потокам, которые ещё есть; удалить state_file.

        Нечего возвращать — pactl не зовём. Не удалось получить список потоков —
        set пробуем вслепую для каждого запомненного."""
        saved, percent, before = self._saved, self._percent, self._before_speech()
        self._saved = {}
        self._percent = None
        self._lowered_once = False
        self._stalled = False
        self._first_seen = {}
        if saved:
            present = self._present()
            self._apply(saved, present)
            if present is not None and percent is not None:
                alive = {entry["name"] for index, entry in saved.items()
                         if present.get(index) == (entry["name"], entry["pid"])}
                self._add_orphans(saved, present, percent, before, alive)
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

    def _before_speech(self) -> set:
        """Ключи потоков, звучавших до начала речи (первый список duck)."""
        return {key for key, seen in self._first_seen.items() if seen == float("-inf")}

    def _orphan_gone(self, foreign: dict[int, dict], before: set) -> None:
        """Запомненные потоки, которых нет в списке, уходят из запомненных (и могут стать сиротой)."""
        present = {index: (entry["name"], entry["pid"]) for index, entry in foreign.items()}
        gone = {index: entry for index, entry in self._saved.items()
                if present.get(index) != (entry["name"], entry["pid"])}
        if not gone:
            return
        for index in gone:
            del self._saved[index]
        self._write_state()
        alive = {entry["name"] for entry in self._saved.values()}
        self._add_orphans(gone, present, self._percent, before, alive)

    def _add_orphans(self, gone: dict[int, dict], present: dict, percent: int, before: set,
                     alive: set) -> None:
        """Сирота — пара (исходная, сниженная) приложения, чей последний приглушённый поток исчез.

        gone — исчезнувшие запомненные потоки, alive — имена приложений, у которых запомненный
        поток жив (пара ещё действует, сирота не нужна). known — потоки приложения, звучавшие
        до начала речи (before — их ключи index+имя+pid): сирота их не трогает; всё, что
        появилось позже (в том числе в этом же проходе), — кандидат."""
        added = False
        for index, entry in gone.items():
            name = entry["name"]
            if name is None or name in alive:
                continue
            known = [[i, pid] for i, (n, pid) in present.items() if n == name and (i, n, pid) in before]
            ducked = [round(v * percent / 100) for v in entry["volumes"]]
            self._orphans[name] = {"original": entry["volumes"], "ducked": ducked, "known": known}
            added = True
        if added:
            self._write_orphans()

    def _live_matches(self, unsaved: dict[int, dict], before: set) -> dict[int, list[int]]:
        """Новые (после начала речи) потоки с громкостью == сниженной ±1 у запомненного потока того
        же приложения: {index: исходная громкость этого запомненного}."""
        result = {}
        for index, entry in unsaved.items():
            if entry["name"] is None or self._key(index, entry) in before:
                continue
            for saved in self._saved.values():
                if saved["name"] != entry["name"]:
                    continue
                ducked = [round(v * self._percent / 100) for v in saved["volumes"]]
                if self._is_close(entry["volumes"], ducked):
                    result[index] = saved["volumes"]
                    break
        return result

    def _orphan_matches(self, streams: dict[int, dict]) -> dict[int, str]:
        """Потоки, которым достаётся сирота: {index: имя сироты}. Выданные сироты не снимаются.

        Поток приложения-сироты «новый», если его нет среди known сироты. Новые с громкостью
        == ducked ±1 получают сироту все; новые есть, но подходящих нет — сирота снимается
        (громкость другая — поправил пользователь, либо число каналов другое)."""
        matches = {}
        dropped = False
        for name, orphan in list(self._orphans.items()):
            known = {tuple(item) for item in orphan["known"]}
            new = {index: entry for index, entry in streams.items()
                   if entry["name"] == name and (index, entry["pid"]) not in known}
            if not new:
                continue
            close = [i for i, e in new.items() if self._is_close(e["volumes"], orphan["ducked"])]
            if not close:
                log.info("Громкость %s другая (или другое число каналов), сирота снята", name)
                del self._orphans[name]
                dropped = True
            for index in close:
                matches[index] = name
        if dropped:
            self._write_orphans()
        return matches

    def _consume(self, names: set[str]) -> None:
        """Снять сирот, которым громкость выставлена."""
        gone = [name for name in names if self._orphans.pop(name, None) is not None]
        if gone:
            self._write_orphans()

    @staticmethod
    def _is_close(current: list[int], ducked: list[int]) -> bool:
        return len(current) == len(ducked) and all(abs(a - b) <= 1 for a, b in zip(current, ducked))

    def _load_orphans(self) -> dict[str, dict]:
        if not self._orphans_file.exists():
            return {}
        try:
            data = json.loads(self._orphans_file.read_text(encoding="utf-8"))
            return {str(name): self._parse_orphan(entry) for name, entry in data.items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("Файл сирот %s повреждён, удалён: %s", self._orphans_file, exc)
            try:
                self._orphans_file.unlink(missing_ok=True)
            except OSError as error:
                log.warning("Не удалён %s: %s", self._orphans_file, error)
            return {}

    def _parse_orphan(self, entry: dict) -> dict:
        """Запись сироты из файла; негодные типы — TypeError."""
        lists = {key: list(entry[key]) for key in ("original", "ducked")}
        known = [[i, pid] for i, pid in entry["known"]]
        valid = (all(self._is_int(v) for values in lists.values() for v in values)
                 and all(self._is_int(i) and (pid is None or isinstance(pid, str)) for i, pid in known))
        if not valid:
            raise TypeError("негодные типы в записи сироты")
        return {**lists, "known": known}

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

    def _lower(self, streams: dict[int, dict], originals: dict[int, list[int]] | None = None,
               consume: set[str] | None = None) -> None:
        """Запомнить потоки в state_file и снизить их на self._percent.

        originals — исходные громкости потоков, которым их выдала пара приложения
        (запоминается она, а не текущая); consume — сироты, которых снять, если все
        громкости выставлены."""
        if not streams:
            return
        originals = originals or {}
        streams = {index: ({**entry, "volumes": originals[index]} if index in originals else entry)
                   for index, entry in streams.items()}
        saved_before = dict(self._saved)
        self._saved.update(streams)
        if not self._write_state():
            self._saved = saved_before  # без записи recover не вернёт громкость — не снижаем
            return
        done = True
        for index, entry in streams.items():
            values = [round(v * self._percent / 100) for v in entry["volumes"]]
            if self._pactl("set-sink-input-volume", str(index), *map(str, values)) is None:
                done = False
        if done and consume:
            self._consume(consume)

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
