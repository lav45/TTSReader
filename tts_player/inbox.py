"""Забор предложений из буфера inbox/ в store/ плеера."""
import logging
import os
import re
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio  # noqa: E402

from tts_player.model import UNKNOWN_TASK, Sentence  # noqa: E402
from tts_player.runtime_dir import APP_DIR, inbox_dir  # noqa: E402

log = logging.getLogger("tts_player.inbox")

# Имя файла от приёмника `tts-player sink` (контракт (13)): <recv_ns>_<task_id>_<имя WAV демона>.
# Без <task_id>_ — имя приёмника прежнего вида (файл пережил обновление в inbox/): id "unknown".
NAME_PATTERN = re.compile(
    r"^(?P<recv_ns>\d{19})_(?:(?P<task_id>[0-9a-f]{1,64}|" + UNKNOWN_TASK + r")_)?"
    r"tts_queue_(?P<session_id>.+)_(?P<token>[0-9a-f]+)_s(?P<index>\d+)\.wav$"
)


def parse_sentence(path: Path) -> Sentence | None:
    """Предложение по имени файла (NAME_PATTERN); имя не по шаблону — None."""
    match = NAME_PATTERN.match(path.name)
    if match is None:
        return None
    return Sentence(
        recv_ns=int(match["recv_ns"]),
        task_id=match["task_id"] or UNKNOWN_TASK,
        session_id=match["session_id"],
        token=match["token"],
        index=int(match["index"]),
        path=path,
    )


class Inbox:
    """Следит за inbox/ и отдаёт предложения в on_sentence по порядку приёма.

    Файл переносится в store/ (rename на одной tmpfs) под тем же именем.
    Имена с точкой в начале (.part приёмника) не читаются; имя не по NAME_PATTERN остаётся
    в inbox/ с WARNING. Имя прежнего вида, без id задачи, забирается с id "unknown".

    store/ принадлежит запущенному плееру: start() удаляет остатки прошлого запуска, кроме
    keep — файлов сообщений, сохранённых им при выходе (messages.json).

    Порядок: накопленное при start() отдаётся по возрастанию recv_ns; дальше доставка
    идёт в порядке событий каталога. Упорядочивает по Sentence.recv_ns потребитель
    (очередь), Inbox заново не сортирует.
    """

    def __init__(self, runtime_dir: Path, on_sentence: Callable[[Sentence], None]) -> None:
        self._inbox = inbox_dir(Path(runtime_dir))
        self._store = Path(runtime_dir) / APP_DIR / "store"
        self._on_sentence = on_sentence
        self._monitor: Gio.FileMonitor | None = None

    @property
    def store(self) -> Path:
        """Каталог предложений этого запуска."""
        return self._store

    def start(self, keep: frozenset[Path] = frozenset()) -> None:
        self._inbox.mkdir(parents=True, exist_ok=True)
        self._store.mkdir(parents=True, exist_ok=True)
        self._clear_store(keep)
        # Сначала монитор, потом скан: файл между ними не потеряется,
        # а повторный забор того же имени пропускается.
        monitor = Gio.File.new_for_path(str(self._inbox)).monitor_directory(
            Gio.FileMonitorFlags.WATCH_MOVES, None
        )
        monitor.connect("changed", self._on_event)
        self._monitor = monitor
        self._scan()

    def stop(self) -> None:
        if self._monitor is not None:
            self._monitor.cancel()
            self._monitor = None

    def _clear_store(self, keep: frozenset[Path]) -> None:
        with os.scandir(self._store) as entries:
            leftovers = [entry.path for entry in entries
                         if entry.is_file() and Path(entry.path) not in keep]
        if not leftovers:
            return
        log.warning("В store/ остались файлы прошлого запуска (%d), удаляю", len(leftovers))
        for path in leftovers:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError:
                log.exception("Не удалось удалить остаток %s", path)

    def _scan(self) -> None:
        with os.scandir(self._inbox) as entries:
            names = [entry.name for entry in entries if entry.is_file()]
        matched = []
        for name in names:
            if name.startswith("."):
                continue
            match = NAME_PATTERN.match(name)
            if match is None:
                log.warning("Имя не подходит под шаблон предложения, остаётся в inbox: %s", name)
                continue
            matched.append((int(match["recv_ns"]), name))
        for _, name in sorted(matched):
            self._take(name)

    def _on_event(self, monitor, file, other_file, event_type) -> None:
        if event_type == Gio.FileMonitorEvent.RENAMED and other_file is not None:
            target = other_file
        elif event_type in (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.MOVED_IN):
            target = file
        else:
            return
        name = target.get_basename()
        if name.startswith("."):
            return
        if NAME_PATTERN.match(name) is None:
            log.warning("Имя не подходит под шаблон предложения, остаётся в inbox: %s", name)
            return
        self._take(name)

    def _take(self, name: str) -> None:
        target = self._store / name
        try:
            self._rename(name, target)
        except FileNotFoundError:
            return  # уже забран (скан и событие монитора могут совпасть)
        except OSError:
            log.exception("Не удалось забрать %s из inbox", name)
            return
        sentence = parse_sentence(target)
        try:
            self._on_sentence(sentence)
        except Exception:
            log.exception("on_sentence упал на %s", name)

    def _rename(self, name: str, target: Path) -> None:
        try:
            os.rename(self._inbox / name, target)
        except FileNotFoundError:
            if self._store.is_dir():
                raise  # пропал сам файл
            log.warning("store/ пропал, создаю заново")
            self._store.mkdir(parents=True, exist_ok=True)
            os.rename(self._inbox / name, target)
