"""Атомарная запись JSON-файла: временный файл рядом + os.replace.

Общий помощник DaemonSettings и PlayerSettings. Ошибки ввода-вывода (OSError) не
оборачивает — текст для экрана делает вызывающий."""
import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


class FileGone(Exception):
    """Файла нет, а create=False: его удалили между чтением и записью."""


def write_json(path: Path, data: dict, create: bool = True) -> None:
    """Временный файл в каталоге path + os.replace; любой сбой до замены убирает временный.

    Права существующего файла сохраняются, у нового 0644. create=False: файла уже
    нет — FileGone, не воскрешаем. Каталог не создаёт."""
    tmp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        try:
            mode = path.stat().st_mode & 0o777
        except FileNotFoundError:
            if not create:
                raise FileGone(path) from None
            mode = 0o644
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                log.debug("Не удалён временный файл %s", tmp_path)
