"""Модель данных плеера."""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Sentence:
    """Предложение, уже перенесённое в store/."""
    recv_ns: int
    session_id: str
    token: str
    index: int
    path: Path


@dataclass
class Message:
    """Сообщение: предложения одного (session_id, token) по возрастанию recv_ns."""
    session_id: str
    token: str
    sentences: list[Sentence]
