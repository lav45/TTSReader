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

log = logging.getLogger("tts_player.inbox")

# Имя файла от обёртки paplay (контракт (13)): <recv_ns>_<task_id>_<имя WAV демона>.
NAME_PATTERN = re.compile(
    r"^(?P<recv_ns>\d{19})_(?P<task_id>[0-9a-f]{1,64}|" + UNKNOWN_TASK + r")"
    r"_tts_queue_(?P<session_id>.+)_(?P<token>[0-9a-f]+)_s(?P<index>\d+)\.wav$"
)


class Inbox:
    """Следит за inbox/ и отдаёт предложения в on_sentence по порядку приёма.

    Файл переносится в store/ (rename на одной tmpfs) под тем же именем.
    Имена с точкой в начале (.part обёртки) не читаются; имя не по NAME_PATTERN (в том
    числе прежнего вида, без id задачи) остаётся в inbox/ с WARNING.

    store/ принадлежит одному запуску плеера: start() удаляет остатки прошлого (после
    падения), штатный выход возвращает незаконченное в inbox/ через put_back().

    Порядок: накопленное при start() отдаётся по возрастанию recv_ns; дальше доставка
    идёт в порядке событий каталога. Упорядочивает по Sentence.recv_ns потребитель
    (очередь), Inbox заново не сортирует.
    """

    def __init__(self, runtime_dir: Path, on_sentence: Callable[[Sentence], None]) -> None:
        base = Path(runtime_dir) / "tts-player"
        self._inbox = base / "inbox"
        self._store = base / "store"
        self._on_sentence = on_sentence
        self._monitor: Gio.FileMonitor | None = None

    @property
    def store(self) -> Path:
        """Каталог предложений этого запуска; сюда же очередь копирует склейки."""
        return self._store

    def start(self) -> None:
        self._inbox.mkdir(parents=True, exist_ok=True)
        self._store.mkdir(parents=True, exist_ok=True)
        self._clear_store()
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

    def put_back(self, sentences: list[Sentence]) -> None:
        """Возвращает предложения из store/ в inbox/ под тем же именем: их заберёт следующий запуск.

        Звать после stop(), иначе монитор заберёт их обратно.
        """
        for sentence in sentences:
            try:
                os.rename(sentence.path, self._inbox / sentence.path.name)
            except OSError:
                log.exception("Не удалось вернуть %s в inbox", sentence.path.name)

    def _clear_store(self) -> None:
        with os.scandir(self._store) as entries:
            leftovers = [entry.path for entry in entries if entry.is_file()]
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
        match = NAME_PATTERN.match(name)
        target = self._store / name
        try:
            self._rename(name, target)
        except FileNotFoundError:
            return  # уже забран (скан и событие монитора могут совпасть)
        except OSError:
            log.exception("Не удалось забрать %s из inbox", name)
            return
        sentence = Sentence(
            recv_ns=int(match["recv_ns"]),
            task_id=match["task_id"],
            session_id=match["session_id"],
            token=match["token"],
            index=int(match["index"]),
            path=target,
        )
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
