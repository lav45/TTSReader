"""Процесс-счётчик предложений: на сколько предложений демон claude-tts разделит текст.

Запускается интерпретатором claude-tts: `<python> -P sentence_count_worker.py` (-P: каталог
скрипта не в sys.path); живёт, пока открыт stdin. Каждая строка stdin — запрос
{"text": ...} в UTF-8; на каждый в stdout сразу строка {"count": n, "rest_at": k} — по
порядку запросов. n — как у демона (daemon.py stream_message: split_sentences текста задачи), а
пустой текст — 0: демон его не синтезирует. k — позиция в тексте, с которой начинается второе
предложение, или null, если предложений меньше двух: text[:k] демон делит на одно первое
предложение, text[k:] — на остальные (граница предложения — пробел после знака конца, и
обе части делятся по тем же границам). Конец stdin — выход 0; непонятный запрос — {"error": ...} и
выход 1; не удалось импортировать claude_code_tts — {"error": ...} и выход 2. Импортирует
только stdlib и claude_code_tts: tts_player в окружении claude-tts недоступен.
"""
import json
import sys

EXIT_OK = 0
EXIT_BAD_REQUEST = 1
EXIT_IMPORT_FAILED = 2


def emit(message: dict) -> None:
    """Одна строка JSON в stdout, буфер сброшен сразу: плеер ждёт ответа."""
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    try:
        from claude_code_tts.bridge import split_sentences
    except Exception as error:
        emit({"error": f"import claude_code_tts: {error!r}"})
        return EXIT_IMPORT_FAILED
    for line in sys.stdin.buffer:
        try:
            request = json.loads(line.decode("utf-8", errors="replace"))
            text = request["text"]
            if not isinstance(text, str):
                raise TypeError(f"text не строка: {text!r}")
        except (ValueError, TypeError, KeyError) as error:
            emit({"error": f"запрос {line[:80]!r}: {error!r}"})
            return EXIT_BAD_REQUEST
        sentences = split_sentences(text) if text.strip() else []
        emit({"count": len(sentences), "rest_at": rest_at(text, sentences)})
    return EXIT_OK


def rest_at(text: str, sentences: list[str]) -> int | None:
    """Позиция второго предложения в text: за концом первого; меньше двух — None."""
    if len(sentences) < 2:
        return None
    first_end = text.index(sentences[0]) + len(sentences[0])
    return text.index(sentences[1], first_end)


if __name__ == "__main__":
    sys.exit(main())
