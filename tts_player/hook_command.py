"""Хуки Claude Code: `tts-player-hook <событие>`, JSON хука — на stdin. Только стандартная
библиотека и лёгкие модули плеера: хук зовут на каждом событии.

События (записи в ~/.claude/settings.json вписывает install.sh):
  start          SessionStart: гейт режима, демон claude-tts, плеер, `session add`;
  end            SessionEnd: `session clear` при reason clear (/clear), иначе `session remove`;
  stop           Stop: речь ответа — `claude-tts speak --from-hook --hook-type stop`;
  post-tool-use  PostToolUse: промежуточная речь — то же с --hook-type post_tool_use;
  pre-tool-use   PreToolUse вопроса (AskUserQuestion): та же промежуточная речь до показа вопроса —
                 PostToolUse вопроса приходит только после ответа; следом — вопросы и варианты
                 из tool_input (`claude-tts speak <текст>`);
  prompt         UserPromptSubmit: тон голоса в контекст — `claude-tts handy tone-context`.

start. Гейт режима: в ~/.claude-tts/config.json mode не queue (нет файла, нечитаемый файл или
нет поля — считается direct) — одна строка в hook.log, код 0, ничего не запускается. Демона нет
(PID из ~/.claude-tts/daemon.pid не жив или чужой: в его команде нет claude-tts и
claude_code_tts) — `claude-tts daemon start`. Звук демон отдаёт плееру по player_command из
config.json: его читают на каждое предложение, перезапуск демона не нужен. Плеер не запущен
(проба блокировки экземпляра) — `tts-player --tray` из этого же репозитория отвязанным процессом
(новая сессия процессов, вывод в player.log); хук его не ждёт. Затем сессия — в список плеера.

stop, post-tool-use, pre-tool-use, prompt. Гейт сессии: нет session_id или сессии нет в списке
запущенного плеера — выход 0 молча. Иначе команда claude-tts получает вход хука как есть, её
stdout — stdout хука (prompt добавляет в контекст тон), код — код хука. pre-tool-use сначала ждёт, пока в
transcript ляжет строка с tool_use_id из входа, не дольше TOOL_USE_WAIT: хук приходит раньше,
чем Claude Code допишет сообщение с вопросом (замер 2026-10-22: строки нет при +0,2 с, есть при
+0,5 с), а claude-tts без новых строк ждёт лишь 0,6 с и, найдя чужие новые строки, не ждёт вовсе.
Не дождался — речь отдаётся как есть, текст прозвучит в PostToolUse после ответа. Повтора нет:
claude-tts двигает водяной знак transcript за прочитанное, а вопросы читает только pre-tool-use.
CLAUDE_TTS_SESSION — UUID сессии Claude
Code (настройки сессий claude-tts ведутся по нему); у речи ещё CLAUDE_TTS_QUEUE_DIR — очередь хука
плеера: задачу забирает плеер, хранит текст и сам кладёт её демону.

stdout start и end пуст всегда (stdout SessionStart попал бы в контекст Claude): строки
состояния дописываются в <runtime>/tts-player/hook.log, первой строкой каждого вызова start и
end — событие `<дата время> start|end <UUID> source=… reason=… cwd=…`; ошибки — в stderr.
Код 1: неверные аргументы (stdin не читается), stdin не один JSON-объект, нет session_id (у start
и cwd) непустой строкой, у end с reason clear нет cwd, нет claude-tts в PATH (кроме end), нет
каталога состояния, запрос в список сессий не записан.
"""
import contextlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from shutil import which

from tts_player import session_command
from tts_player.instance import is_locked
from tts_player.runtime_dir import APP_DIR, MISSING, hook_queue_dir, runtime_dir

USAGE = "Usage: tts-player-hook start|end|stop|post-tool-use|pre-tool-use|prompt (JSON хука на stdin)"
PLAYER_BIN = Path(__file__).resolve().parent.parent / "bin" / "tts-player"
SPEECH = {"stop": "stop", "post-tool-use": "post_tool_use", "pre-tool-use": "post_tool_use"}
DAEMON_MARKS = ("claude-tts", "claude_code_tts")
TOOL_USE_WAIT = 10.0
TOOL_USE_POLL = 0.05


class HookError(Exception):
    """Сбой хука: сообщение — в stderr, код 1."""


def main(args: list[str], stdin=None, home: Path | None = None,
         player_bin: Path = PLAYER_BIN) -> int:
    if len(args) != 1 or args[0] not in ("start", "end", "prompt", *SPEECH):
        print(f"tts-player-hook: {USAGE}", file=sys.stderr)
        return 1
    event = args[0]
    home = Path.home() if home is None else home
    try:
        raw = (sys.stdin if stdin is None else stdin).read()
        if event in ("start", "end"):
            return session_event(event, parse(raw), home, player_bin)
        return voiced_event(event, raw)
    except HookError as error:
        print(f"tts-player-hook: {error}", file=sys.stderr)
        return 1


def parse(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise HookError("stdin не один JSON-объект")
    return data


def field(data: dict, key: str) -> str:
    """Поле строкой или пусто."""
    value = data.get(key)
    return value if isinstance(value, str) else ""


def state_dir() -> Path:
    runtime = runtime_dir()
    if runtime is None:
        raise HookError(MISSING)
    state = runtime / APP_DIR
    try:
        state.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise HookError(f"не удалось создать {state}: {error}") from error
    return state


def session_event(event: str, data: dict, home: Path, player_bin: Path) -> int:
    state = state_dir()
    session_id = field(data, "session_id")
    if not session_id:
        raise HookError("в JSON нет session_id")
    log = Log(state / "hook.log")
    # по строке события видно, с каким cwd пришли SessionEnd и следующий SessionStart
    # (замена сессии при /clear сравнивает их каталоги)
    log(f"{time.strftime('%F %T')} {event} {session_id} source={field(data, 'source')}"
        f" reason={field(data, 'reason')} cwd={field(data, 'cwd')}")
    cwd = field(data, "cwd")
    if event == "start":
        if not cwd:
            raise HookError("в JSON нет cwd")
        return start(session_id, cwd, home, state, log, player_bin)
    if field(data, "reason") == "clear":
        if not cwd:
            raise HookError("в JSON нет cwd (reason clear)")
        return session_request(log, "session clear", session_command.session_clear, session_id, cwd)
    return session_request(log, "session remove", session_command.session_remove, session_id)


def start(session_id: str, cwd: str, home: Path, state: Path, log: "Log", player_bin: Path) -> int:
    claude_tts = which("claude-tts")
    if claude_tts is None:
        raise HookError("claude-tts не найден в PATH")
    if claude_tts_mode(home) != "queue":
        log("режим claude-tts не queue — плеер не участвует, сессия не добавлена")
        return 0
    if daemon_alive(home):
        log("демон уже запущен")
    else:
        subprocess.run([claude_tts, "daemon", "start"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("демон запущен" if daemon_alive(home) else
            "демон не запустился, см. ~/.claude-tts/daemon.log")
    if is_locked(state.parent):
        log("плеер уже запущен")
    else:
        with open(state / "player.log", "ab") as player_log:
            subprocess.Popen([str(player_bin), "--tray"], stdin=subprocess.DEVNULL,
                             stdout=player_log, stderr=player_log, start_new_session=True)
        log(f"запуск плеера отдан (ошибки - {state / 'player.log'})")
    return session_request(log, "session add", session_command.session_add, session_id, cwd)


def claude_tts_mode(home: Path) -> str:
    """mode из config.json claude-tts; нет файла, нечитаемый файл или нет поля — direct."""
    try:
        config = json.loads((home / ".claude-tts" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "direct"
    mode = config.get("mode") if isinstance(config, dict) else None
    return mode if isinstance(mode, str) else "direct"


def daemon_alive(home: Path) -> bool:
    """PID из daemon.pid жив и это claude-tts: номер умершего демона мог занять чужой процесс."""
    try:
        pid = int((home / ".claude-tts" / "daemon.pid").read_text().strip())
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True,
                                 text=True).stdout
    except OSError:
        return False
    return any(mark in command for mark in DAEMON_MARKS)


def session_request(log: "Log", what: str, request: Callable[..., int], *args: str) -> int:
    """Запрос в список сессий плеера; его строки — в hook.log, не в stdout хука."""
    with log.open() as out, contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = request(*args)
    if code != 0:
        raise HookError(f"{what} не выполнен, см. {log.path}")
    return 0


def voiced_event(event: str, raw: str) -> int:
    """stop, post-tool-use, pre-tool-use, prompt: команда claude-tts, только для сессии из списка плеера."""
    try:
        data = json.loads(raw)
    except ValueError:
        return 0
    session_id = field(data, "session_id") if isinstance(data, dict) else ""
    runtime = runtime_dir()
    if not session_id or runtime is None or session_command.session_has(session_id) != 0:
        return 0
    claude_tts = which("claude-tts")
    if claude_tts is None:
        raise HookError("claude-tts не найден в PATH")
    env = {**os.environ, "CLAUDE_TTS_SESSION": session_id}
    if event not in SPEECH:
        return subprocess.run([claude_tts, "handy", "tone-context"], input=raw, text=True, env=env).returncode
    env["CLAUDE_TTS_QUEUE_DIR"] = str(hook_queue_dir(runtime))
    if event == "pre-tool-use":
        wait_for_tool_use(field(data, "transcript_path"), field(data, "tool_use_id"))
    command = [claude_tts, "speak", "--from-hook", "--hook-type", SPEECH[event]]
    code = subprocess.run(command, input=raw, text=True, env=env).returncode
    questions = questions_text(data.get("tool_input")) if event == "pre-tool-use" else ""
    if questions:
        subprocess.run([claude_tts, "speak", questions], input="", text=True, env=env)
    return code


def wait_for_tool_use(transcript_path: str, tool_use_id: str) -> None:
    """Ждёт строку с tool_use_id в transcript, читая только дописанное после входа в хук."""
    if not transcript_path or not tool_use_id:
        return
    needle = tool_use_id.encode()
    deadline = time.monotonic() + TOOL_USE_WAIT
    try:
        with open(transcript_path, "rb") as transcript:
            # хвост на случай, если строка легла до открытия файла
            transcript.seek(max(0, transcript.seek(0, os.SEEK_END) - 65536))
            seen = b""
            while True:
                seen = seen[-len(needle):] + transcript.read()
                if needle in seen or time.monotonic() >= deadline:
                    return
                time.sleep(TOOL_USE_POLL)
    except OSError:
        return


def questions_text(tool_input: object) -> str:
    """Вопросы AskUserQuestion с вариантами: «Вопрос? Варианты: А, Б.» подряд."""
    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    if not isinstance(questions, list):
        return ""
    parts = []
    for question in questions:
        if not isinstance(question, dict):
            continue
        options = question.get("options")
        labels = [field(option, "label") for option in options if isinstance(option, dict)] \
            if isinstance(options, list) else []
        labels = [label for label in labels if label]
        text = field(question, "question")
        if labels:
            text += f" Варианты: {', '.join(labels)}."
        if text:
            parts.append(text)
    return " ".join(parts)


class Log:
    """hook.log: строка на вызов log(text); open() — для вывода запросов в список сессий."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __call__(self, text: str) -> None:
        with self.open() as out:
            out.write(text + "\n")

    def open(self):
        return open(self.path, "a", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
