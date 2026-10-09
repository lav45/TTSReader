"""Забор файлов *.json из каталога-почтового ящика по порядку имён: queue/ хука и control/."""
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio  # noqa: E402

SUFFIX = ".json"
LEFTOVER_SUFFIX = ".tmp"
# Свежий *.tmp может писать CLI прямо сейчас: хук запускает плеер и сразу зовёт `session add`,
# а старт плеера чистит остатки. Остаток убитого писателя — только старше этого возраста.
LEFTOVER_MIN_AGE_SECONDS = 60


class Mailbox:
    """Следит за каталогом и отдаёт содержимое файлов в handle(name, raw) по порядку имён.

    Берутся *.json без точки в начале; *.tmp писатель ещё пишет, а его rename в .json приходит
    как RENAMED. Файл удаляется после handle (упавший handle не оставляет файл: иначе его
    подобрал бы следующий запуск повторно); нечитаемый удаляется со строкой WARNING.
    Разбор содержимого и его битые случаи — дело handle. clean_leftovers: start() сначала
    удаляет *.tmp старше LEFTOVER_MIN_AGE_SECONDS (остатки писателя, убитого между mkstemp и replace) — одним явным шагом.
    Журнал и приставка строк (label) — владельца ящика.
    """

    def __init__(self, directory: Path, handle: Callable[[str, str], None],
                 logger: logging.Logger, label: str, *, clean_leftovers: bool = False) -> None:
        self._directory = Path(directory)
        self._handle = handle
        self._log = logger
        self._label = label
        self._clean_leftovers = clean_leftovers
        self._monitor: Gio.FileMonitor | None = None

    def start(self) -> None:
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self._log.warning("%s: каталог %s не создан: %s", self._label, self._directory, error)
            return
        # Сначала монитор, потом скан: файл между ними не потеряется,
        # а повторный забор того же имени пропускается.
        monitor = Gio.File.new_for_path(str(self._directory)).monitor_directory(
            Gio.FileMonitorFlags.WATCH_MOVES, None
        )
        monitor.connect("changed", self._on_event)
        self._monitor = monitor
        if self._clean_leftovers:
            self._remove_leftovers()
        self.drain()

    def stop(self) -> None:
        if self._monitor is not None:
            self._monitor.cancel()
            self._monitor = None

    def drain(self) -> None:
        """Забрать всё, что лежит сейчас, по порядку имён, без цикла GLib."""
        try:
            with os.scandir(self._directory) as entries:
                names = [entry.name for entry in entries if entry.is_file()]
        except OSError as error:
            self._log.warning("%s: каталог %s не читается: %s", self._label, self._directory,
                              error)
            return
        for name in sorted(names):
            if self._wanted(name):
                self._take(name)

    def _remove_leftovers(self) -> None:
        threshold = time.time() - LEFTOVER_MIN_AGE_SECONDS
        for stale in self._directory.glob("*" + LEFTOVER_SUFFIX):
            try:
                if stale.stat().st_mtime >= threshold:
                    continue
                stale.unlink(missing_ok=True)
            except OSError as error:
                self._log.warning("%s: остаток %s не удалён: %s", self._label, stale.name, error)

    def _on_event(self, monitor, file, other_file, event_type) -> None:
        if event_type == Gio.FileMonitorEvent.RENAMED and other_file is not None:
            target = other_file
        elif event_type in (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.MOVED_IN):
            target = file
        else:
            return
        name = target.get_basename()
        if self._wanted(name):
            self._take(name)

    @staticmethod
    def _wanted(name: str) -> bool:
        return name.endswith(SUFFIX) and not name.startswith(".")

    def _take(self, name: str) -> None:
        path = self._directory / name
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return  # уже забран (скан и событие монитора могут совпасть)
        except (OSError, UnicodeDecodeError) as error:
            self._log.warning("%s: %s не читается (%s), удаляю", self._label, name, error)
            self._remove(path)
            return
        try:
            self._handle(name, raw)
        except Exception:
            self._log.exception("%s: обработчик упал на %s", self._label, name)
        self._remove(path)

    def _remove(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            self._log.exception("Не удалось удалить %s", path.name)
