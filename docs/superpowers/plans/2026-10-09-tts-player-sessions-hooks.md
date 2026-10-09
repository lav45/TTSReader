# TTS Player — хуки регистрируют сессию, гейт спрашивает плеер. План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/tts-on` добавляет сессию в список плеера, `/tts-off` убирает; гейт озвучки
пропускает хуки сессий из списка и передаёт им `CLAUDE_TTS_SESSION=<UUID>`; флаг
`/tmp/claude-tts.on` и команды `tts-player stop`/`play` уходят.

**Architecture:** в репозитории — `bin/tts-player-hook on <uuid> <cwd>`: демон и плеер, как
раньше, но вместо `tts-player play` в конце `tts-player session add <uuid> <cwd>`; подкоманда
`off` убирается. `tts-player stop`/`play` (сигналы SIGUSR1/SIGUSR2) теряют единственного
потребителя и удаляются вместе со своей обвязкой. Вне репозитория (согласие пользователя
получено 2026-10-09; копии `*.bak-…` рядом) — `~/.claude/hooks/tts-gate.sh`,
`tts-toggle.sh`, `tts-command.sh` и строка `description` в `~/.claude/commands/tts-on.md`,
`tts-off.md` — по черновикам, показанным пользователю.

**Tech Stack:** bash, Python 3.12; новых зависимостей нет.

**Slice:** стык «`tts-toggle.sh` → `tts-player-hook`» (часть цепочки (14)) и гейт — читатель (15).
Третьим: стоит на влитых (15) и (14); раньше сессий внутри плеера, иначе живая установка
осталась бы без звука (решение при планировании, в спеке).

**Next slices (not planned here):** сессии внутри плеера (свой хвост, синтез только активной,
переключение); окно (список сессий, кнопки активной).

**Spec:** `docs/superpowers/specs/2026-10-09-tts-player-sessions-design.md`

**Assumptions:**
- Хук Claude Code запускается в каталоге сессии (`$PWD` == `cwd` входа хука) — проверяется в
  Task 3 по входу настоящего хука (строка `cwd` из `transcript` текущей сессии), до правки.
- `tts-player` есть в `PATH` хуков: `tts-toggle.sh` уже зовёт `tts-player-hook` из `PATH`, обе
  ссылки лежат в `~/.local/bin` — не риск.
- `CLAUDE_TTS_SESSION` подменяет сессию `claude-tts` (`cli.py:1905`, факт спеки) — не риск.
- Плеер на живой установке перезапущен после слияния среза (14) (с согласия пользователя) и
  ведёт `sessions.json` — проверяется в Task 3 до переключения гейта.

## Global Constraints

- Цепочка спеки: `/tts-on → tts-toggle.sh on → tts-player-hook on → tts-player session add <UUID> <cwd>`;
  `/tts-off → tts-toggle.sh off → tts-player session remove <UUID>`.
- `/tts-on` и `/tts-off` воспроизведение не трогают: `tts-player-hook` не зовёт `play`/`stop`.
- Гейт: `tts-player session has <UUID>` вместо флага; хук запускается с
  `HOME=hook-home` (сам скрипт `speak-*.sh`, не меняется) и `CLAUDE_TTS_SESSION=<UUID>`.
- Правки вне репозитория — только с копией `<файл>.bak-2026-10-09` рядом (есть уже — суффикс
  `.bak-2026-10-09-sessions`), содержимое — черновики из согласования, без изменений по
  существу; изменение по существу — остановиться и спросить.
- Тесты `tts-player-hook` не трогают настоящие `~/.claude-tts`, `/proc` и `$XDG_RUNTIME_DIR`
  (`TTS_HOOK_PROC`, `TTS_HOOK_HOME`, заглушки в `PATH` — как в `tests/test_player_hook.py`).
- Коммит по путям, без `Co-Authored-By`; без `git push`.

**User decisions (already made):**
- Хуки вне репозитория правятся в этом срезе без новой остановки (2026-10-09).
- Плеер после слияний, меняющих формат между процессами, перезапускаю сам: SIGTERM, затем
  `setsid tts-player --tray`, вывод в `player.log` (2026-10-09).
- `/tts-on` и `/tts-off` только включают и выключают запись текста своей сессии.

## Файлы

| Файл | Кто создаёт / кто вызывает | Назначение |
|---|---|---|
| `bin/tts-player-hook` | `~/.claude/hooks/tts-toggle.sh` | демон, плеер, `session add` |
| `tts_player/__main__.py`, `tts_player/instance.py`, `tts_player/player.py`, `tts_player/text_speaker.py` | — | удаление `stop`/`play` и их обвязки |
| `tests/test_player_hook.py`, `tests/test_stop_command.py`, `tests/test_player_e2e.py`, `tests/test_instance.py`, `tests/test_player.py`, `tests/test_text_speaker.py` | `unittest discover` | хук; удаление тестов `stop`/`play` |
| `README.md` | читатель | цепочка `/tts-on`/`/tts-off`, гейт, переход |
| `~/.claude/hooks/tts-gate.sh`, `tts-toggle.sh`, `tts-command.sh`, `~/.claude/commands/tts-on.md`, `tts-off.md` | Claude Code | вне репозитория, по черновикам |

---

### Task 1: tts-player-hook on регистрирует сессию

**Goal:** `tts-player-hook on <uuid> <cwd>` поднимает демон и плеер, как раньше, и добавляет
сессию в список плеера; `play` больше не зовёт; `off` убран.

**Files:**
- Modify: `bin/tts-player-hook`
- Test: `tests/test_player_hook.py`

**Contract:** вход `tts-player-hook`, который зовёт `tts-toggle.sh`

```
tts-player-hook on <uuid> <cwd>
  демон: нет живого PID → claude-tts daemon start; жив без daemon-bin первым в PATH →
         setsid claude-tts daemon restart; жив с daemon-bin → не трогать   (как сейчас)
  плеер: не запущен → setsid tts-player --tray (вывод в player.log); запущен → ничего
  затем: tts-player session add <uuid> <cwd> — его stdout в stdout хука
  exit 0; 1 — нет claude-tts или tts-player в PATH, нет XDG_RUNTIME_DIR, неверные аргументы
tts-player-hook off | on без <uuid> <cwd> | что-то другое → «Usage: tts-player-hook on <uuid> <cwd>», exit 1
```

**Acceptance Criteria:**
- [ ] плеер запущен (заглушка `tts-player` в `PATH` пишет свои аргументы в файл) → вызов `tts-player session add u1 /p/X`, вызова `tts-player play` нет; stdout содержит вывод заглушки
- [ ] плеер не запущен → `setsid tts-player --tray`, затем `session add u1 /p/X`
- [ ] ветки демона (start / restart / не трогать) — прежние тесты зелёные
- [ ] `off`, `on`, `on u1` → usage, exit 1, `tts-player` не вызван
- [ ] шапка скрипта описывает новый вызов и не упоминает `play`/`stop`

**Verify:** `python3 -m unittest tests.test_player_hook -v` → `OK`

**Steps:**

- [ ] **Step 1: Failing acceptance** — тест «плеер запущен → `session add`» с заглушкой `tts-player`. Падает: хук зовёт `play`.
- [ ] **Step 2: Правка скрипта** — против контракта выше; тесты `off` и `play` переписать под новый контракт, а не удалить молча.
- [ ] **Step 3: Verify** — команда выше, затем весь набор `python3 -m unittest discover -s tests`.
- [ ] **Step 4: Commit** — `bin/tts-player-hook tests/test_player_hook.py`.

---

### Task 2: Удалить tts-player stop/play и их обвязку

**Goal:** команды `stop`/`play`, потерявшие последнего потребителя, удалены вместе с
обработчиками сигналов и методами, которые жили только ради них.

**Files:**
- Modify: `tts_player/__main__.py`, `tts_player/instance.py`, `tts_player/player.py`, `tts_player/text_speaker.py`, `README.md`
- Delete/Modify tests: `tests/test_stop_command.py`, `tests/test_player_e2e.py`, `tests/test_instance.py`, `tests/test_player.py`, `tests/test_text_speaker.py`

**Contract:** контракт среза — вход `tts-player-hook` (Task 1); у удаления своего артефакта нет.
Что остаётся у CLI:

```
tts-player [--audio-sink …] [--no-gui|--show|--tray] …     запуск плеера
tts-player session add|remove|has …                         команда сессии (14), (15)
tts-player stop | play                                      → не подкоманды: argparse, exit 2
```

**Acceptance Criteria:**
- [ ] `grep -rn "send_stop\|send_play\|play_if_stopped\|stop_playback\|SIGUSR" tts_player bin` пуст (или каждое оставшееся место названо в отчёте с потребителем)
- [ ] `tts-player stop` → exit 2 (argparse), плеер не трогается
- [ ] блокировка и `holder_pid` / `is_locked` остаются (их зовут запуск и `session has`) — их тесты зелёные
- [ ] метод, удаляемый как «только для stop/play», не зовётся окном (`grep` по `tts_player/window.py`) — подтверждено в отчёте
- [ ] README не упоминает `tts-player stop`/`play`
- [ ] README, раздел «Список сессий»: «запрос не пишется» при «уже в списке»/«не в списке» — с оговоркой «только при пустой `control/`» (остаток финального ревью среза (14))
- [ ] README, раздел перехода: какие файлы вне репозитория правятся, порядок (слияние → перезапуск плеера → хуки → `/tts-on` в каждой нужной сессии), цепочка `/tts-on`/`/tts-off` и гейт по `session has`
- [ ] весь набор зелёный

**Verify:** `python3 -m unittest discover -s tests` → `OK`

**Steps:**

- [ ] **Step 1: Список потребителей** — `grep` по `tts_player`, `bin`, `tests`, `~/.claude/hooks`, `~/.claude/commands`; в отчёт — кто зовёт каждое удаляемое имя. Удаляется только то, у кого после Task 1 потребителей нет.
- [ ] **Step 2: Удалить** — код, тесты, строки README; докстринги, ссылающиеся на `tts-player stop`/`play`, поправить.
- [ ] **Step 2a: README** — оговорка в «Списке сессий» и раздел перехода (критерии выше).
- [ ] **Step 3: Verify** — весь набор.
- [ ] **Step 4: Commit** — изменённые и удалённые пути, по именам.

---

### Task 3: Переключение хуков на живой установке

**Goal:** после слияния ветки в `master` хуки Claude Code регистрируют сессии и гейт спрашивает
плеер; текущая озвучиваемая сессия (из флага) уже в списке.

**Исполнитель:** контроллер после слияния ветки в `master` (не исполнитель в worktree).

**Files:**
- Modify (вне репозитория): `~/.claude/hooks/tts-gate.sh`, `~/.claude/hooks/tts-toggle.sh`, `~/.claude/hooks/tts-command.sh`, `~/.claude/commands/tts-on.md`, `~/.claude/commands/tts-off.md`

**Contract:** гейт — читатель (15) и источник `CLAUDE_TTS_SESSION`

```
tts-gate.sh <hook-script>   (вход хука в stdin)
  session_id пуст или `tts-player session has <session_id>` ≠ 0 → exit 0 молча
  иначе CLAUDE_TTS_SESSION=<session_id> exec <hook-script> (вход без изменений)
tts-toggle.sh on  → режим queue: tts-player-hook on "$CLAUDE_CODE_SESSION_ID" "$PWD"
tts-toggle.sh off → tts-player session remove "$CLAUDE_CODE_SESSION_ID"
tts-command.sh /tts-on → немедленная озвучка с CLAUDE_TTS_SESSION=<session_id>
```

**Acceptance Criteria:**
- [ ] рядом с каждым правленым файлом — копия `*.bak-2026-10-09` (у `tts-toggle.sh` — `*.bak-2026-10-09-sessions`), совпадающая с прежним содержимым (`cmp`)
- [ ] `bash -n` всех трёх скриптов — без ошибок; `diff` с черновиками — пусто
- [ ] процесс плеера запущен после слияния среза (14) и ведёт `control/` (`ps -o lstart`)
- [ ] сессия из `/tmp/claude-tts.on` добавлена: `tts-player session has <uuid>` → 0; флаг удалён
- [ ] `echo '{"session_id":"<чужой uuid>"}' | tts-gate.sh voice-context.sh` → exit 0 без запуска; с `<uuid>` из списка и заглушечным скриптом — скрипт видит `CLAUDE_TTS_SESSION=<uuid>`

**Verify:** `tts-player session has "$(cat <сохранённый uuid флага>)"; echo $?` → `0`

**Steps:**

- [ ] **Step 1: Процесс плеера** — запущен после слияния (14) (`ps -o lstart`, есть `control/`); срез меняет в процессе плеера только удаление `stop`/`play`, перезапуск не нужен.
- [ ] **Step 2: Проверить предпосылку `$PWD` == `cwd`** — по входу настоящего хука текущей сессии; не сошлось — остановиться и спросить (изменение черновика по существу).
- [ ] **Step 3: Копии и правка** — `cp -p` в `*.bak-…`, затем черновики на место; `bash -n`, `diff`.
- [ ] **Step 4: Сессия из флага** — `cwd` сессии из её транскрипта (`~/.claude/projects/*/<uuid>.jsonl`, поле `cwd`), `tts-player session add <uuid> <cwd>`, проверить `has`, удалить флаг.
- [ ] **Step 5: Проверка гейта** — критерии выше; итог — в отчёт о слиянии.
