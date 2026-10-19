"""Настройки claude-tts для плеера: `python3 -m tts_player.claude_tts_config <tts-player>`.

Зовёт install.sh. <tts-player> — абсолютный путь к bin/tts-player (ссылке на него). В
~/.claude-tts/config.json ставятся mode = queue (хуки отдают речь демону через плеер) и
player_command = [<tts-player>, "sink"] (демон отдаёт звук плееру, а не играет сам). Персона
SUPERTONIC (модель Supertonic 3, русский) добавляется, если её нет, и тогда же становится
активной; уже настроенная не меняется. Остальные ключи файла не трогаются. Нет файла — выход 1:
его создаёт установщик claude-tts. Запись атомарная. В stdout — что сделано.
"""
import json
import os
import sys
from pathlib import Path

from tts_player.claude_settings import write_atomic

SUPERTONIC = "supertonic-3"
SUPERTONIC_PERSONA = {
    "description": "Supertonic 3, голос 3, русский с английскими словами (lang=ru)",
    "voice_sherpa": SUPERTONIC,
    "speaker_sherpa": 3,
    "speed": 1.7,
    "speed_method": "length_scale",
    "max_chars": 10000,
    "ai_type": "claude",
}


def player_config(config: dict, player: str) -> dict:
    """config с настройками плеера; исходный dict не меняется."""
    result = {**config, "mode": "queue", "player_command": [player, "sink"]}
    personas = dict(result.get("personas") or {})
    if SUPERTONIC not in personas:
        personas[SUPERTONIC] = SUPERTONIC_PERSONA
        result["personas"] = personas
        result["active_persona"] = SUPERTONIC
    return result


def main(args: list[str], config_path: Path | None = None) -> int:
    if len(args) != 1 or not os.path.isabs(args[0]):
        print("usage: python3 -m tts_player.claude_tts_config <абсолютный путь к tts-player>",
              file=sys.stderr)
        return 2
    path = Path.home() / ".claude-tts" / "config.json" if config_path is None else config_path
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"{path} не прочитан ({error}): сначала установщик claude-tts", file=sys.stderr)
        return 1
    if not isinstance(config, dict):
        print(f"{path} не объект JSON", file=sys.stderr)
        return 1
    updated = player_config(config, args[0])
    if updated == config:
        print(f"настройки плеера уже в {path}")
        return 0
    write_atomic(path, json.dumps(updated, indent=2, ensure_ascii=False) + "\n")
    print(f"настройки плеера вписаны в {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
