"""Процесс-счётчик предложений: на сколько предложений демон claude-tts разделит текст.

Запускается интерпретатором claude-tts: `<python> -P sentence_count_worker.py` (-P: каталог
скрипта не в sys.path); живёт, пока открыт stdin. Каждая строка stdin — запрос
{"text": ...} в UTF-8; на каждый в stdout сразу строка {"count": n, "starts": [k0, …]} — по
порядку запросов. n — как у демона (daemon.py stream_message: split_sentences текста задачи), а
пустой текст — 0: демон его не синтезирует. starts — позиции в тексте, с которых начинаются
предложения, по порядку (n штук): кусок text[k_i:k_{i+1}] (последний — до конца) демон делит
ровно на одно предложение i (граница предложения — пробел после знака конца, внутри предложения
такой границы нет). Конец stdin — выход 0; непонятный запрос — {"error": ...} и
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
        emit({"count": len(sentences), "starts": starts(text, sentences)})
    return EXIT_OK


def starts(text: str, sentences: list[str]) -> list[int]:
    """Позиции предложений в text по порядку: каждое ищется за концом предыдущего."""
    found = []
    at = 0
    for sentence in sentences:
        at = text.index(sentence, at)
        found.append(at)
        at += len(sentence)
    return found


if __name__ == "__main__":
    sys.exit(main())
