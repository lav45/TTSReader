"""Процесс синтеза «Своего текста»: режет текст на предложения и пишет WAV по одному.

Запускается интерпретатором claude-tts: `<python> synth_worker.py <out_dir>`.
Текст читается из stdin целиком. В stdout идут строки JSON (каждая сразу сбрасывается):
{"count": k}, затем {"index": i, "path": ..., "duration": ...} по порядку, затем
{"done": true}; при сбое синтеза предложения — {"error": ...} и выход с кодом 1.
Выход 2 — не удалось импортировать claude_code_tts. Импортирует только stdlib и
claude_code_tts: tts_player в окружении claude-tts недоступен.
"""
import json
import sys
import wave
from pathlib import Path

EXIT_OK = 0
EXIT_SYNTH_FAILED = 1
EXIT_IMPORT_FAILED = 2


def emit(message: dict) -> None:
    """Печатает одну строку JSON и сразу сбрасывает буфер."""
    print(json.dumps(message, ensure_ascii=False), flush=True)


def wav_duration(path: Path) -> float:
    """Длительность WAV в секундах."""
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / wav.getframerate()


def main(argv: list[str]) -> int:
    """Синтезирует предложения текста из stdin в WAV-файлы каталога argv[1]."""
    out_dir = Path(argv[1])
    try:
        from claude_code_tts.bridge import split_sentences
        from claude_code_tts.config import load_raw_config
        from claude_code_tts.daemon import daemon_generate_speech
    except ImportError as error:
        emit({"error": f"import claude_code_tts: {error}"})
        return EXIT_IMPORT_FAILED

    text = sys.stdin.read()
    sentences = split_sentences(text) if text.strip() else []
    emit({"count": len(sentences)})
    if sentences:
        out_dir.mkdir(parents=True, exist_ok=True)
        persona = load_raw_config()["active_persona"]
        for index, sentence in enumerate(sentences):
            path = out_dir / f"{index}.wav"
            try:
                ok = daemon_generate_speech(sentence, persona, path)
            except Exception as error:
                emit({"error": f"предложение {index}: {error}"})
                return EXIT_SYNTH_FAILED
            if not ok:
                emit({"error": f"предложение {index}: синтез не удался"})
                return EXIT_SYNTH_FAILED
            emit({"index": index, "path": str(path), "duration": wav_duration(path)})
    emit({"done": True})
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv))
