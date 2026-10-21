"""Хуки плеера в ~/.claude/settings.json: `python3 -m tts_player.claude_settings <hook-command>`.

Зовёт install.sh. <hook-command> — абсолютный путь к tts-player-hook. Для каждого события
Claude Code (EVENTS) запись плеера ставится одна; прежние записи озвучки убираются: свои
(tts-player-hook), установщика claude-tts (speak-response.sh и другие его скрипты, `claude-tts
speak --from-hook`, `claude-tts supersede --from-hook` — те же признаки, что у claude_code_tts
install.TTS_HOOK_MARKERS) и гейт прежней установки плеера (tts-gate.sh). Хук в записи вместе с
чужими хуками убирается из неё один; запись, оставшаяся пустой, убирается целиком. Чужие хуки и
остальные ключи файла не меняются. Перед записью — копия settings.json.bak-<дата время>, если файл
был; запись атомарная, права файла сохраняются. В stdout — что сделано.
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# событие Claude Code → (аргумент tts-player-hook, таймаут, async, matcher)
EVENTS: dict[str, tuple[str, int, bool, str | None]] = {
    "SessionStart": ("start", 5, False, None),
    "SessionEnd": ("end", 5, False, None),
    "Stop": ("stop", 180, True, "*"),
    "PostToolUse": ("post-tool-use", 30, True, "*"),
    # PostToolUse вопроса приходит только после ответа: текст перед вопросом озвучивается до него
    "PreToolUse": ("pre-tool-use", 30, True, "AskUserQuestion"),
    "UserPromptSubmit": ("prompt", 5, False, "*"),
}
MARKERS = (
    "tts-player-hook", "tts-gate.sh",
    "speak-response.sh", "speak-intermediate.sh", "play-sound.sh", "voice-context.sh",
    "prompt-submitted.sh", "claude-tts speak --from-hook", "claude-tts supersede --from-hook",
)


def is_voice_hook(hook: object) -> bool:
    command = hook.get("command", "") if isinstance(hook, dict) else ""
    return isinstance(command, str) and any(marker in command for marker in MARKERS)


def install_hooks(settings: dict, hook_command: str) -> dict:
    """settings с записями плеера вместо прежних записей озвучки; исходный dict не меняется."""
    result = dict(settings)
    hooks = dict(result.get("hooks") or {})
    for event, (argument, timeout, is_async, matcher) in EVENTS.items():
        kept = []
        for entry in hooks.get(event) or []:
            if not isinstance(entry, dict):
                kept.append(entry)
                continue
            inner = [h for h in entry.get("hooks") or [] if not is_voice_hook(h)]
            if inner:
                kept.append({**entry, "hooks": inner})
            elif not entry.get("hooks"):
                kept.append(entry)
        hook: dict = {"type": "command", "command": f"{hook_command} {argument}", "timeout": timeout}
        if is_async:
            hook["async"] = True
        entry = {"hooks": [hook]} if matcher is None else {"matcher": matcher, "hooks": [hook]}
        hooks[event] = [*kept, entry]
    result["hooks"] = hooks
    return result


def main(args: list[str], settings_path: Path | None = None) -> int:
    if len(args) != 1 or not os.path.isabs(args[0]):
        print("usage: python3 -m tts_player.claude_settings <абсолютный путь к tts-player-hook>",
              file=sys.stderr)
        return 2
    path = Path.home() / ".claude" / "settings.json" if settings_path is None else settings_path
    try:
        settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError as error:
        print(f"{path} не JSON ({error}) — хуки не вписаны", file=sys.stderr)
        return 1
    if not isinstance(settings, dict):
        print(f"{path} не объект JSON — хуки не вписаны", file=sys.stderr)
        return 1
    updated = install_hooks(settings, args[0])
    if updated == settings:
        print(f"хуки плеера уже в {path}")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y-%m-%d-%H%M%S')}")
        shutil.copy2(path, backup)
        print(f"копия: {backup}")
    write_atomic(path, json.dumps(updated, indent=2, ensure_ascii=False) + "\n")
    print(f"хуки плеера вписаны в {path}")
    return 0


def write_atomic(path: Path, text: str) -> None:
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
