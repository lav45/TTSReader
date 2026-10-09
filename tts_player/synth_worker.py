"""Процесс синтеза «Своего текста»: режет текст на предложения и пишет WAV по одному.

Запускается интерпретатором claude-tts: `<python> synth_worker.py <out_dir>`.
Текст читается из stdin целиком, UTF-8. В stdout идут строки JSON в UTF-8 (каждая сразу
сбрасывается):
{"count": k}, затем {"index": i, "path": ..., "duration": ...} по порядку, затем
{"done": true}; при сбое синтеза предложения — {"error": ...} и выход с кодом 1.
Выход 2 — не удалось импортировать claude_code_tts. Импортирует только stdlib и
claude_code_tts: tts_player в окружении claude-tts недоступен.
"""
import json
import os
import sys
import wave
from pathlib import Path

EXIT_OK = 0
EXIT_SYNTH_FAILED = 1
EXIT_IMPORT_FAILED = 2


PROTOCOL_STREAM = sys.stdout  # main() заменяет его копией fd 1, пока fd 1 не ушёл в stderr


def emit(message: dict) -> None:
    """Печатает одну строку JSON в поток протокола и сразу сбрасывает буфер."""
    PROTOCOL_STREAM.write(json.dumps(message, ensure_ascii=False) + "\n")
    PROTOCOL_STREAM.flush()


def wav_duration(path: Path) -> float:
    """Длительность WAV в секундах."""
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / wav.getframerate()


def synthesize(sentence: str, persona: str, path: Path, synthesize_speech) -> float:
    """Пишет предложение в `<i>.part.wav`, переименовывает в path после успеха; возвращает длительность.

    Недописанный файл (убитый процесс) остаётся под именем .part.wav и не выглядит готовым.
    """
    partial = path.with_name(f"{path.stem}.part.wav")
    try:
        if not synthesize_speech(sentence, persona, partial):
            raise RuntimeError("синтез не удался")
        duration = wav_duration(partial)
        partial.replace(path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return duration


def protect_protocol() -> None:
    """Протокол — в копию fd 1; сам fd 1 и sys.stdout — в stderr.

    Посторонний вывод claude_code_tts (print) и его дочерних процессов, унаследовавших fd 1,
    уходит в stderr и протокол не портит.
    """
    global PROTOCOL_STREAM
    sys.stdout.flush()
    protocol_fd = os.dup(1)
    PROTOCOL_STREAM = os.fdopen(protocol_fd, "w", encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr


def main(argv: list[str]) -> int:
    """Синтезирует предложения текста из stdin в WAV-файлы каталога argv[1]."""
    protect_protocol()
    out_dir = Path(argv[1])
    try:
        from claude_code_tts.bridge import split_sentences
        from claude_code_tts.config import load_raw_config
        from claude_code_tts.daemon import daemon_generate_speech
    except Exception as error:
        emit({"error": f"import claude_code_tts: {error!r}"})
        return EXIT_IMPORT_FAILED

    text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    sentences = split_sentences(text) if text.strip() else []
    emit({"count": len(sentences)})
    if sentences:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            persona = load_raw_config()["active_persona"]
        except Exception as error:
            emit({"error": f"голос не определён: {error!r}"})
            return EXIT_SYNTH_FAILED
        if not persona:
            emit({"error": "голос не определён: active_persona пуст"})
            return EXIT_SYNTH_FAILED
        for index, sentence in enumerate(sentences):
            path = out_dir / f"{index}.wav"
            try:
                duration = synthesize(sentence, persona, path, daemon_generate_speech)
            except Exception as error:
                emit({"error": f"предложение {index}: {error}"})
                return EXIT_SYNTH_FAILED
            emit({"index": index, "path": str(path), "duration": duration})
    emit({"done": True})
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv))
