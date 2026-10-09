# TTS Player — сессия попадает в плеер по SessionStart и уходит по SessionEnd. План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `bin/tts-player-hook start|end` читает JSON хука Claude Code со stdin и поднимает демон
и плеер с `session add` (start) или делает `session remove` (end); `/tts-on`, `/tts-off` и их
обвязка вне репозитория уходят, хуки SessionStart/SessionEnd прописаны в `~/.claude/settings.json`.

**Architecture:** `tts-player-hook` теряет режим `on <uuid> <cwd>` и получает `start` и `end`:
разбор входа хука (`jq`, как в `tts-gate.sh`) — в нём, под тестами `tests/test_player_hook.py`.
Строки состояния, которые `on` печатал в stdout, уходят в `$XDG_RUNTIME_DIR/tts-player/hook.log`:
stdout хука SessionStart Claude Code добавляет в контекст модели. Упоминания `/tts-on`/`/tts-off`
в коде, окне и README переписываются на хуки сессии. Вне репозитория — после слияния в master:
две записи в `settings.json`, удаление `tts-command.sh` (UserPromptSubmit), `tts-toggle.sh`,
`commands/tts-on.md`, `tts-off.md` с копиями `*.bak-2026-10-09-auto`.

**Tech Stack:** bash, `jq` (уже нужен гейту `tts-gate.sh`, `/usr/bin/jq`), Python 3.12 unittest;
новых зависимостей нет.

**Slice:** контракт «`tts-player-hook start|end` ← хуки SessionStart и SessionEnd» — единственный
контракт спеки; поведение плеера по фокусу стоит на нём (сессии появляются сами), а не наоборот.

**Next slices (not planned here):** поведение плеера по фокусу — длина истории без фокуса,
синтез только при автовоспроизведении, ▶ без фокуса — последнее, клик по сессии без фокуса,
выключатель автовоспроизведения, автозапуск нового ответа (решения спеки; стыка-контракта нет,
меняется `Player`).

**Spec:** `docs/superpowers/specs/2026-10-09-tts-player-auto-sessions-design.md`

**Assumptions:**
- Вход хуков проверен по документации Claude Code (`code.claude.com/docs/en/hooks`, 2026-10-09):
  общие поля `session_id`, `transcript_path`, `cwd`, `hook_event_name`; у SessionStart `source`
  ∈ `startup|resume|clear|compact|fork`, у SessionEnd `reason` ∈
  `clear|resume|logout|prompt_input_exit|other`. Plain-text stdout SessionStart добавляется в
  контекст модели; код, отличный от 0 и 2, — неблокирующая ошибка, пользователю показывается
  первая строка stderr. SessionEnd по умолчанию — 1,5 с на все хуки; `timeout` хука поднимает
  бюджет до 60 с.
- SessionEnd при `/resume` (reason `resume`) убирает текущую сессию, SessionStart возобновлённой
  (`source: resume`) добавляет её — повторный add ничего не меняет (`Sessions.add` → False) — не
  риск, поведение плеера уже есть.
- `session remove` при незапущенном плеере кладёт запрос в `control/`, плеер применит его при
  запуске (`session_command.py`, `offline_text`) — не риск.
- `jq` в `PATH` хуков и тестов (`/usr/bin`) — не риск: им уже пользуется гейт.
- Живая установка: `~/.local/bin/tts-player-hook` — ссылка в рабочее дерево master; до правки
  `settings.json` нынешний `/tts-on` зовёт `tts-player-hook on`, после слияния режима `on` нет —
  поэтому Task 3 идёт сразу после слияния, одним заходом.

## Global Constraints

- Цепочка спеки: `SessionStart → tts-player-hook start` (демон, плеер, `tts-player session add
  <session_id> <cwd>`); `SessionEnd → tts-player-hook end` (`tts-player session remove
  <session_id>`). Стык `session add|remove → control/` не меняется.
- `tts-player-hook` ничего не пишет в stdout ни в одном режиме, включая ошибки.
- Поведение демона и плеера в `start` — как у прежнего `on`: старт/перезапуск демона с
  `daemon-bin` первым в `PATH`, `setsid` для перезапуска и плеера, хук ничего не ждёт.
- Тесты не трогают настоящие `~/.claude-tts`, `/proc`, `$XDG_RUNTIME_DIR` (`TTS_HOOK_PROC`,
  `TTS_HOOK_HOME`, заглушки в `PATH` — как сейчас в `tests/test_player_hook.py`).
- Правки вне репозитория — только с копией `<файл>.bak-2026-10-09-auto` рядом; удаление файла —
  переименование в такую копию.
- Коммит по путям, без `Co-Authored-By`; без `git push`.

**User decisions (already made):**
- `/tts-on` и `/tts-off` убираются; сессия добавляется в плеер при запуске сессии Claude Code.
- SessionEnd (выход, `/clear`, `/resume`) убирает любую сессию, в том числе активную —
  пользователь выбрал это вместо моей рекомендации «кроме активной».
- SessionStart поднимает демон и плеер тем же `tts-player-hook`; гейт `tts-gate.sh` остаётся.
- Разбор входа хука — в `tts-player-hook` (`start`/`end`), не скриптом в `~/.claude/hooks`.
- Строки состояния — в `hook.log`, не в stdout; в stderr — только ошибка с кодом 1.
- Сессии, запущенные до перехода, молчат до перезапуска — шаг в README.

## Файлы

| Файл | Кто вызывает | Назначение |
|---|---|---|
| `bin/tts-player-hook` | хуки SessionStart/SessionEnd из `~/.claude/settings.json` | `start`/`end` вместо `on` |
| `tests/test_player_hook.py` | `python3 -m unittest` | тесты `start`/`end` вместо `on` |
| `tts_player/player.py:293` | — | докстринг `_on_hook_task`: `/tts-on` → SessionStart |
| `tts_player/window.py:145`, `tests/gui_window_cases.py:225` | окно | подсказка «Завершить»: без `(/tts-off)` |
| `tests/test_player_e2e.py:50` | — | докстринг: «как `/tts-on`» → «как SessionStart» |
| `README.md` | пользователь | команды, переход на хуки сессии |

---

### Task 1: `tts-player-hook start|end` с входом хука на stdin

**Goal:** `tts-player-hook start` и `end` принимают JSON хука Claude Code и делают то, что делали
`on` и `tts-toggle.sh off`; stdout пуст, строки состояния — в `hook.log`.

**Files:**
- Modify: `bin/tts-player-hook`
- Test: `tests/test_player_hook.py`

**Contract:** `tts-player-hook start|end` ← хуки SessionStart и SessionEnd

```
tts-player-hook start     # хук SessionStart
  stdin:  {"session_id": "<uuid>", "cwd": "<path>", "source": "startup|resume|clear|compact|fork", …}
  делает: демон (нет — claude-tts daemon start; жив без daemon-bin первым в PATH — restart через
          setsid), плеер (не запущен — tts-player --tray через setsid), затем
          tts-player session add <session_id> <cwd>          # как прежний `on`, ничего не ждёт
tts-player-hook end       # хук SessionEnd
  stdin:  {"session_id": "<uuid>", "reason": "clear|resume|logout|prompt_input_exit|other", …}
  делает: tts-player session remove <session_id>
stdout:   пусто
журнал:   $XDG_RUNTIME_DIR/tts-player/hook.log — строки состояния (демон, плеер, вывод session
          add|remove, предупреждение о перезапуске демона), дописываются
код:      0; 1 + строка в stderr — stdin не JSON-объект, нет session_id (у start и cwd) непустой
          строкой, нет claude-tts (start) или tts-player в PATH, нет XDG_RUNTIME_DIR,
          session add|remove вернул не 0
          иные аргументы — usage в stderr, код 1, ничего не запускается и stdin не читается
тесты:    TTS_HOOK_PROC, TTS_HOOK_HOME — как сейчас
```

**Acceptance Criteria:**
- [ ] `start` с `{"session_id":"u1","cwd":"/p/proj","source":"startup"}`: заглушка `tts-player`
      получила `session add u1 /p/proj`; демон и плеер — как в нынешних тестах `on` (старт с
      обёрткой, restart чужого PATH, обёртка первой — не трогать, `--tray`, хук не ждёт плеер)
- [ ] `end` с `{"session_id":"u1","reason":"clear"}`: заглушка `tts-player` получила
      `session remove u1`, `claude-tts` не вызывался, плеер не запускался
- [ ] stdout пуст в каждом тесте модуля, включая ошибки
- [ ] `hook.log` содержит строки состояния (`демон запущен`, `плеер уже запущен`, вывод заглушки
      `session add`/`session remove`); предупреждение о перезапуске демона есть и без фразы
      `/tts-on`
- [ ] не JSON, JSON-массив, нет `session_id`, пустой `session_id`, у `start` нет `cwd` → код 1,
      строка в stderr, заглушки не вызывались
- [ ] `on u1 /p`, без аргументов, `start extra` → usage в stderr, код 1, ничего не запускается
- [ ] `session add|remove` заглушки с кодом 1 → хук с кодом 1
- [ ] нет `tts-player` в PATH → код 1 у `start` и `end`; нет `claude-tts` → код 1 у `start`,
      `end` работает

**Verify:** `python3 -m unittest tests.test_player_hook -v` → `OK`

**Steps:**

- [ ] **Step 1: Failing acceptance** — переписать тесты `on` на `start` с JSON на stdin
  (`run_hook` получает вход), добавить тесты `end`, пустого stdout, `hook.log` и разбора входа;
  убедиться, что падают из-за отсутствия режимов, а не опечатки.
- [ ] **Step 2: Implement** — по контракту выше; шапку скрипта переписать под `start`/`end`
  (кто вызывает — хуки SessionStart/SessionEnd); режим `on` и фраза «повторите /tts-on» уходят.
- [ ] **Step 3: Verify** — команда выше и полный `python3 -m unittest discover -s tests` → `OK`.
- [ ] **Step 4: Commit** — `bin/tts-player-hook`, `tests/test_player_hook.py`.

### Task 2: Упоминания `/tts-on`/`/tts-off` в коде, окне и README

**Goal:** в репозитории нет `/tts-on`/`/tts-off` как действующих команд: докстринги и подсказка
окна говорят о хуках сессии, README описывает установку хуков SessionStart/SessionEnd и переход.

**Files:**
- Modify: `tts_player/player.py:293`, `tts_player/window.py:145`, `tests/gui_window_cases.py:225`,
  `tests/test_player_e2e.py:50`, `README.md`

**Contract:** обслуживает контракт `tts-player-hook start|end`; своего артефакта нет.

```
README «Команды»:  tts-player-hook start | end — вход хука SessionStart | SessionEnd на stdin
README «Переход на хуки сессии»: копии *.bak-2026-10-09-auto; записи SessionStart/SessionEnd в
                   ~/.claude/settings.json (строки — Task 3); убрать строку tts-command.sh из
                   UserPromptSubmit; tts-command.sh, tts-toggle.sh, commands/tts-on.md, tts-off.md —
                   в копии; сессии, запущенные до перехода, молчат до перезапуска; откат
окно:              подсказка кнопки «Завершить» — «Завершить активную сессию»
```

**Acceptance Criteria:**
- [ ] `grep -rn "tts-on\|tts-off\|tts-toggle\|tts-command" bin tts_player tests README.md` находит
      только историю перехода в README (раздел прежнего перехода и откат), не действующие
      команды
- [ ] GUI-тест подсказки «Завершить» проходит с новым текстом
- [ ] README «Команды» описывает `tts-player-hook start|end`, «Список сессий» — что сессию
      добавляет SessionStart и убирает SessionEnd или «Завершить»

**Verify:** `python3 -m unittest discover -s tests` → `OK`, GUI-случаи не пропущены

**Steps:**

- [ ] **Step 1: Правка текста** — по списку файлов; разделы README о прежнем переходе на сессии
  (флаг, `tts-toggle.sh`) переписать как историю или заменить разделом нового перехода — по
  контракту выше.
- [ ] **Step 2: Verify** — команда выше и `grep` из критериев.
- [ ] **Step 3: Commit** — файлы задачи по путям.

### Task 3: Переключение живой установки на хуки сессии (после слияния в master)

**Goal:** `~/.claude/settings.json` зовёт `tts-player-hook start|end` на SessionStart/SessionEnd;
`/tts-on`, `/tts-off` и их скрипты убраны в копии; живой хук проверен на настоящем плеере.

**Files:**
- Modify (вне репозитория): `~/.claude/settings.json`
- Move to `*.bak-2026-10-09-auto` (вне репозитория): `~/.claude/hooks/tts-command.sh`,
  `~/.claude/hooks/tts-toggle.sh`, `~/.claude/commands/tts-on.md`, `~/.claude/commands/tts-off.md`

**Contract:** записи хуков — часть контракта `tts-player-hook start|end`

```
~/.claude/settings.json, .hooks:
  "SessionStart": [{"hooks": [{"type": "command",
                    "command": "/home/lav45/.local/bin/tts-player-hook start", "timeout": 5}]}]
  "SessionEnd":   [{"hooks": [{"type": "command",
                    "command": "/home/lav45/.local/bin/tts-player-hook end", "timeout": 5}]}]
  "UserPromptSubmit": без записи /home/lav45/.claude/hooks/tts-command.sh; tts-gate.sh voice-context.sh остаётся
```

**Acceptance Criteria:**
- [ ] `jq '.hooks.SessionStart, .hooks.SessionEnd' ~/.claude/settings.json` показывает записи
      выше; в `UserPromptSubmit` нет `tts-command.sh`; `settings.json.bak-2026-10-09-auto` рядом
- [ ] четыре файла лежат только как `*.bak-2026-10-09-auto`
- [ ] живая проба: `echo '{"session_id":"probe-auto","cwd":"/tmp","source":"startup"}' |
      tts-player-hook start` → код 0, stdout пуст; через 2 с `tts-player session has probe-auto`
      → 0; `echo '{"session_id":"probe-auto","reason":"other"}' | tts-player-hook end` → код 0;
      через 2 с `tts-player session has probe-auto` → 1; строки пробы в `hook.log`

**Verify:** команды пробы из критериев → коды `0`, `0`, `0`, `1`

**Steps:**

- [ ] **Step 1: Проверить момент** — ветка слита в master, `~/.local/bin/tts-player-hook` указывает
  на master с режимами `start`/`end`; плеер запущен (`tts-player session has` отвечает).
- [ ] **Step 2: Копии и правка** — копия `settings.json`, правка по контракту выше (`jq`, атомарно
  через временный файл), переименование четырёх файлов в копии.
- [ ] **Step 3: Живая проба** — по критериям; `hook.log` показать в отчёте.
- [ ] **Step 4: Commit** — правок в репозитории нет; в отчёте — что текущие сессии Claude Code
  остаются в списке до своего SessionEnd, а новые попадают в плеер сами.
