"""Интерпретатор окружения claude-tts: в нём воркеры плеера импортируют claude_code_tts.

Берётся из скрипта `claude-tts` в PATH — так его нашёл бы любой запуск `claude-tts`, где бы uv
ни держал окружение (UV_TOOL_DIR, macOS). Скрипт uv начинается либо с `#!<python>`, либо, когда
путь длинный или с пробелами, с `#!/bin/sh` и строки `'''exec' '<python>' "$0" "$@"`.
"""
import re
import shutil
from collections.abc import Callable
from pathlib import Path

SH_EXEC = re.compile(r"""^'''exec' '(?P<python>[^']+)' """)


def claude_tts_python(which: Callable[[str], str | None] = shutil.which) -> Path | None:
    """Путь интерпретатора или None: claude-tts нет в PATH или скрипт не от uv/pip."""
    script = which("claude-tts")
    if script is None:
        return None
    try:
        with open(script, encoding="utf-8", errors="replace") as f:
            first, second = f.readline().strip(), f.readline().strip()
    except OSError:
        return None
    if not first.startswith("#!"):
        return None
    shebang = first[2:].strip()
    if shebang == "/bin/sh":
        match = SH_EXEC.match(second)
        return Path(match["python"]) if match else None
    if " " in shebang:  # `#!/usr/bin/env python3` и подобное: окружение не названо
        return None
    return Path(shebang)
