"""Сообщения между запусками плеера: $XDG_RUNTIME_DIR/tts-player/messages.json.

Пишет Player.shutdown (снимки очередей всех сессий одним снимком, MessageQueue.release),
читает Player.start до inbox/ и задач хука и раскладывает сообщения по очередям сессий по
их session_id; файл живёт от выхода до следующего старта. Текст и кэш временные:
перезапуск плеера их сохраняет, выход из сеанса ($XDG_RUNTIME_DIR) — нет."""
import json
import logging
from pathlib import Path

from tts_player import atomic_json
from tts_player.inbox import parse_sentence
from tts_player.model import Message
from tts_player.queue import QueueSnapshot

log = logging.getLogger("tts_player.saved_messages")

VERSION = 1


class SavedMessages:
    """messages.json: снимок очереди; файлы предложений — по имени в store/."""

    def __init__(self, path: Path, store: Path) -> None:
        self._path = path
        self._store = store

    def save(self, snapshot: QueueSnapshot) -> None:
        """Атомарная запись; сбой — WARNING (файлы в store/ следующий старт удалит)."""
        started = [snapshot.messages.index(m) for m in snapshot.started]
        data = {
            "version": VERSION,
            "task_order": snapshot.task_order,
            "delivered": snapshot.delivered,
            "messages": [self._to_dict(m, i in started) for i, m in enumerate(snapshot.messages)],
        }
        try:
            atomic_json.write_json(self._path, data)
        except OSError as error:
            log.warning("сообщения не сохранены в %s: %s", self._path, error)

    def load(self) -> QueueSnapshot:
        """Снимок прошлого запуска; файл удаляется. Нет файла — пустой снимок; битый —
        WARNING и пустой снимок. Пропавшие файлы предложений выпадают."""
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return QueueSnapshot([], [], {}, 0)
        except OSError as error:
            log.warning("сообщения прошлого запуска не прочитаны из %s: %s", self._path, error)
            return QueueSnapshot([], [], {}, 0)
        finally:
            self._path.unlink(missing_ok=True)
        try:
            return self._snapshot(json.loads(text))
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            log.warning("%s битый (%r), сообщения прошлого запуска не восстановлены",
                        self._path, error)
            return QueueSnapshot([], [], {}, 0)

    def _snapshot(self, data: dict) -> QueueSnapshot:
        messages: list[Message] = []
        started: list[Message] = []
        for item in data["messages"]:
            message = self._from_dict(item)
            messages.append(message)
            if item["started"]:
                started.append(message)
        task_order = {str(task_id): int(order) for task_id, order in data["task_order"].items()}
        return QueueSnapshot(messages, started, task_order, int(data["delivered"]))

    @staticmethod
    def _to_dict(message: Message, started: bool) -> dict:
        return {
            "session_id": message.session_id,
            "project": message.project,
            "text": message.text,
            "persona": message.persona,
            "speed": message.speed,
            "task_ids": sorted(message.task_ids),
            "received_at": message.received_at,
            "sent": message.sent,
            "heard": message.heard,
            "deferred": message.deferred,
            "started": started,
            "sentences": [s.path.name for s in message.sentences],
            "sentence_count": message.sentence_count,
            "played_indices": sorted(message.played_indices),
            "starts": message.starts,
            "pieces": message.pieces,
        }

    def _from_dict(self, item: dict) -> Message:
        # ключи audio/created_at/duration_s файла до отказа от склейки демона не читаются:
        # её копия в store/ без ссылки удаляется на старте, как любой лишний файл
        return Message(
            session_id=self._text(item["session_id"]),
            project=self._optional_text(item["project"]),
            text=self._optional_text(item["text"]),
            persona=self._optional_text(item["persona"]),
            speed=self._optional_number(item["speed"]),
            task_ids={self._text(task_id) for task_id in item["task_ids"]},
            sentences=self._sentences(item["sentences"]),
            received_at=self._optional_number(item["received_at"]),
            sent=bool(item["sent"]),
            heard=bool(item["heard"]),
            deferred=bool(item.get("deferred", False)),  # файл до сессий в плеере — без ключа
            # файл до счётчика предложений — без ключа: число запросит Player.start
            sentence_count=self._optional_count(item.get("sentence_count")),
            played_indices={self._count(index) for index in item.get("played_indices", [])},
            # файл до текста по предложениям — без ключей: позиции запросит Player.start;
            # первое предложение, отданное заранее (head_task), — отданное предложение 0
            starts=self._optional_starts(item.get("starts")),
            pieces=self._pieces(item),
        )

    def _pieces(self, item: dict) -> dict[str, int]:
        if "pieces" in item:
            pieces = item["pieces"]
            if not isinstance(pieces, dict):
                raise TypeError(f"ожидался словарь: {pieces!r}")
            return {self._text(task_id): self._count(index) for task_id, index in pieces.items()}
        head = self._optional_text(item.get("head_task"))
        return {} if head is None else {head: 0}

    @classmethod
    def _optional_starts(cls, value) -> list[int] | None:
        if value is None:
            return None
        if not isinstance(value, list):
            raise TypeError(f"ожидался список: {value!r}")
        return [cls._count(start) for start in value]

    def _sentences(self, names: list) -> list:
        sentences = []
        for name in names:
            path = self._store / self._text(name)
            sentence = parse_sentence(path)
            if sentence is None or not path.is_file():
                log.info("предложение %s прошлого запуска пропало", name)
                continue
            sentences.append(sentence)
        return sentences

    @staticmethod
    def _text(value) -> str:
        if not isinstance(value, str):
            raise TypeError(f"ожидалась строка: {value!r}")
        return value

    @classmethod
    def _optional_text(cls, value) -> str | None:
        return None if value is None else cls._text(value)

    @classmethod
    def _optional_count(cls, value) -> int | None:
        return None if value is None else cls._count(value)

    @staticmethod
    def _count(value) -> int:
        """Число предложений или индекс: целое ≥ 0."""
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TypeError(f"ожидалось целое ≥ 0: {value!r}")
        return value

    @staticmethod
    def _optional_number(value) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"ожидалось число: {value!r}")
        return float(value)
