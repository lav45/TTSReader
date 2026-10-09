# TTS Player — `/clear` заменяет активную сессию новой из того же каталога. План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** после `/clear` в активной сессии новая сессия Claude Code из того же каталога сама
становится активной в плеере; остальные завершения (выход, `/resume`, «Завершить») — как сейчас.

**Architecture:** `tts-player-hook end` при reason `clear` зовёт новую подкоманду
`tts-player session clear <uuid> <cwd>`; она кладёт в `control/` запрос `{"op": "clear", …}`.
`ControlRequests` разбирает его в `on_clear(session, cwd)`; `Player.clear_session` делает то же,
что `remove_session`, и, если сессия была активной, запоминает `cwd` (в памяти); следующий
`add_session` с тем же `cwd` делает новую сессию активной (`Sessions.select`), выбор пользователем
ожидание снимает. Формат `sessions.json` не меняется.

**Tech Stack:** Python 3.12 unittest, bash, `jq`; новых зависимостей нет.

**Slice:** контракт «`tts-player session clear` → `control/` → плеер (замена при `/clear`)» спеки —
пишущая сторона (подкоманда и хук) и читающая (разбор запроса и плеер) вместе с проверкой через
процесс плеера.

**Next slices (not planned here):** поведение плеера по фокусу (решения спеки о длине истории,
синтезе, ▶, клике по сессии, выключателе автовоспроизведения).

**Spec:** `docs/superpowers/specs/2026-10-09-tts-player-auto-sessions-design.md`

**Assumptions:**
- Порядок хуков при `/clear`: `SessionEnd` (reason `clear`) старой сессии, затем `SessionStart`
  (source `clear`) новой — проверено пробной сессией 2026-10-09 (`hook.log`: add, remove, add,
  remove). Запросы в `control/` идут по порядку имён (`time.time_ns()`) — clear раньше add.
- `cwd` у SessionEnd и следующего SessionStart совпадает, если каталог в сессии не менялся —
  не проверено с `cd`; расхождение спека принимает (замены нет, сессию выбирают кликом). Риск
  принят, проба — Task 4 (строки `session clear`/`session replace` в журнале плеера).
- Работающий плеер со старым кодом удаляет запрос с неизвестным op (`control.py`, WARNING) —
  поэтому после слияния плеер перезапускается (Task 4); решение пользователя из плана
  `2026-10-09-tts-player-sessions-hooks.md`: «Плеер после слияний, меняющих формат между
  процессами, перезапускаю сам: SIGTERM, затем `setsid tts-player --tray`, вывод в `player.log`».

## Global Constraints

- Контракт — раздел «`tts-player session clear` → `control/` → плеер» спеки, дословно ниже в Task 1.
- `Player.clear()` («Очистить» активной сессии) не меняется и не путается с `clear_session`.
- «Завершить» (`remove_session` из окна), выход и `/resume` (reason ≠ `clear`) активную не передают.
- `tts-player-hook` по-прежнему ничего не пишет в stdout; ошибки — stderr, код 1.
- Тесты не трогают настоящие `$XDG_RUNTIME_DIR`, `~/.claude-tts`, `/proc` (как существующие).
- Коммит по путям, без `Co-Authored-By`; без `git push`.

**User decisions (already made):**
- `/clear` — замена: если очищенная сессия была активной, активной становится новая сессия из
  того же `cwd`; тексты очищенной удаляются (2026-10-09).
- Ожидание замены без таймаута: снимают пришедшая замена и выбор сессии пользователем; живёт в
  памяти плеера (2026-10-09).
- Плеер после слияния, меняющего формат между процессами, перезапускаю сам (2026-10-09).

## Файлы

| Файл | Кто вызывает | Назначение |
|---|---|---|
| `tts_player/session_command.py` | `tts-player session …` (хук) | подкоманда `clear` |
| `tts_player/control.py` | `Player` (монитор `control/`) | op `clear` → `on_clear` |
| `tts_player/player.py` | `ControlRequests`, окно | `clear_session`, ожидание замены в `add_session`/`select_session` |
| `bin/tts-player-hook` | хук SessionEnd | reason `clear` → `session clear` |
| `README.md` | пользователь | «Команды», «Список сессий» |

---

### Task 1: Пишущая сторона и разбор: `tts-player session clear` и op `clear` в `control/`

**Goal:** `tts-player session clear UUID CWD` кладёт запрос в `control/`, `ControlRequests` отдаёт
его колбэку `on_clear(session, cwd)`.

**Files:**
- Modify: `tts_player/session_command.py`, `tts_player/control.py`, `tts_player/player.py:140`
  (третий колбэк; пока на `remove_session` через лямбду — до Task 2)
- Test: `tests/test_session_command.py`, `tests/test_control.py`

**Contract:** `tts-player session clear` → `control/` → плеер

```
tts-player-hook end:  reason == "clear" → tts-player session clear <session_id> <cwd>
                      иначе             → tts-player session remove <session_id>
                      у reason clear нет cwd непустой строкой → код 1, строка в stderr
tts-player session clear UUID CWD
  запрос:  control/<ns>.json = {"op": "clear", "session": "<uuid>", "cwd": "<path>"}
           пишется всегда, когда плеер может его не знать (как remove: UUID нет в sessions.json и
           control/ пуст — «сессия не в списке», код 0)
  вывод:   как у remove; неверные аргументы — usage в stderr, код 2
плеер, clear(session, cwd):
           сессии нет в списке — ничего (строка журнала нет)
           иначе — то же, что remove (звук стоп, тексты и файлы удалены; строка «session clear <uuid>»);
           была активной — ожидание замены: pending_cwd = cwd
плеер, add(session, cwd):
           pending_cwd == cwd и сессия новая → она активна, текущего нет, статус idle;
           pending_cwd снят; строка «session replace <uuid>»
плеер, select(session):  pending_cwd снят
битый запрос clear (нет строкового session или cwd) — удаляется, WARNING, как у add

ControlRequests(control_dir, on_add: Callable[[str, str], None], on_remove: Callable[[str], None],
                on_clear: Callable[[str, str], None])
SESSION_USAGE = "usage: tts-player session add <uuid> <cwd> | remove <uuid> | clear <uuid> <cwd> | has <uuid>"
```

**Acceptance Criteria:**
- [ ] `session clear u1 /p` при запущенном плеере (блокировка занята) пишет в `control/` ровно
      `{"op": "clear", "session": "u1", "cwd": "/p"}`, вывод как у `remove`, код 0
- [ ] `session clear u1 /p`, когда `u1` нет в `sessions.json` и `control/` пуст, — «сессия не в
      списке», запрос не пишется, код 0
- [ ] `session clear u1` (без cwd) и `session clear` — usage в stderr, код 2
- [ ] `ControlRequests`: файл `{"op":"clear","session":"u1","cwd":"/p"}` → `on_clear("u1", "/p")`;
      clear без строкового `cwd` или `session` — удалён, WARNING, колбэк не зван
- [ ] порядок имён соблюдается для смеси add/remove/clear

**Verify:** `python3 -m unittest tests.test_session_command tests.test_control -v` → `OK`

**Steps:**

- [ ] **Step 1: Failing acceptance** — тесты по критериям; падают из-за отсутствия `clear`.
- [ ] **Step 2: Implement** — по контракту; общая с `remove` логика «нужен ли запрос» не
  дублируется. В `player.py` — третий аргумент `ControlRequests`, пока ведущий на удаление сессии.
- [ ] **Step 3: Verify** — команда выше и полный `python3 -m unittest discover -s tests` → `OK`.
- [ ] **Step 4: Commit** — файлы задачи по путям.

### Task 2: Читающая сторона: `Player.clear_session` и замена активной в `add_session`

**Goal:** плеер на `clear` активной сессии ждёт сессию из того же `cwd` и делает её активной.

**Files:**
- Modify: `tts_player/player.py` (`clear_session`, `add_session`, `select_session`, колбэк `on_clear`)
- Test: `tests/test_player.py`, `tests/test_player_e2e.py`

**Contract:** часть «плеер, clear / add / select» контракта Task 1

```
Player.clear_session(session_id: str, cwd: str) -> None
  нет в списке → ничего; иначе как remove_session + строка «session clear <uuid>»;
  была активной → self._replace_cwd = cwd
Player.add_session(session_id, cwd): новая сессия и cwd == self._replace_cwd →
  Sessions.select(session_id), текущего нет, статус idle, self._replace_cwd = None,
  строка «session replace <uuid>»
Player.select_session(...): self._replace_cwd = None
```

**Acceptance Criteria:**
- [ ] активная A (cwd `/p`) и неактивная B; `clear_session(A, "/p")`, затем `add_session(C, "/p")`
      → активна C, статус idle, текущего нет, журнал «session clear A», «session replace C»
- [ ] то же, но `add_session(C, "/q")` → активной нет; затем `add_session(D, "/p")` → активна D
- [ ] `clear_session` неактивной B → активная A не меняется; следующий `add_session(C, cwd B)` не
      делает C активной
- [ ] после `clear_session(A, "/p")` клик `select_session(B)` снимает ожидание: `add_session(C, "/p")`
      оставляет активной B
- [ ] `remove_session` активной (как «Завершить») ожидания не заводит: `add_session(C, тот же cwd)`
      — активной нет (прежний тест `test_added_session_after_removed_active_is_not_active_while_list_not_empty` проходит)
- [ ] `clear_session` сессии не из списка — ничего, строки журнала нет
- [ ] e2e через процесс плеера: `session add A /p`, `session add B /q`, `session clear A /p`,
      `session add C /p` → в `sessions.json` `active` = C, в журнале плеера `session replace C`

**Verify:** `python3 -m unittest tests.test_player tests.test_player_e2e -v` → `OK`

**Steps:**

- [ ] **Step 1: Failing acceptance** — тесты по критериям.
- [ ] **Step 2: Implement** — по контракту; общая часть `remove_session` и `clear_session` не
  дублируется; `ControlRequests` в `player.py` получает `self.clear_session`.
- [ ] **Step 3: Verify** — команда выше и полный набор → `OK`, GUI-случаи не пропущены.
- [ ] **Step 4: Commit** — файлы задачи по путям.

### Task 3: Хук `end` и README

**Goal:** `tts-player-hook end` при reason `clear` зовёт `session clear` с `cwd`; README описывает
замену при `/clear`.

**Files:**
- Modify: `bin/tts-player-hook`, `README.md`
- Test: `tests/test_player_hook.py`

**Contract:** часть «tts-player-hook end» контракта Task 1

```
tts-player-hook end:  reason == "clear" → tts-player session clear <session_id> <cwd>
                      иначе             → tts-player session remove <session_id>
                      у reason clear нет cwd непустой строкой → код 1, строка в stderr
```

**Acceptance Criteria:**
- [ ] `end` с `{"session_id":"u1","reason":"clear","cwd":"/p"}` → заглушка `tts-player` получила
      `session clear u1 /p`
- [ ] `end` с reason `other`, `logout`, `resume`, `prompt_input_exit` и без reason → `session remove u1`
- [ ] `end` с reason `clear` без `cwd` или с пустым `cwd` → код 1, stderr, заглушка не вызвана
- [ ] stdout пуст во всех случаях; вывод `session clear` — в `hook.log`
- [ ] README: «Команды» — `session clear UUID CWD`; «Список сессий» — `/clear` в активной сессии
      передаёт активную новой сессии из того же каталога, выход/`/resume`/«Завершить» — нет;
      шапка `bin/tts-player-hook` описывает ветку `clear`

**Verify:** `python3 -m unittest tests.test_player_hook -v` → `OK`

**Steps:**

- [ ] **Step 1: Failing acceptance** — тесты по критериям.
- [ ] **Step 2: Implement** — по контракту; README и шапка скрипта.
- [ ] **Step 3: Verify** — команда выше и полный набор → `OK`.
- [ ] **Step 4: Commit** — файлы задачи по путям.

### Task 4: Живая установка: перезапуск плеера и проба `/clear` (после слияния в master)

**Goal:** работающий плеер получает новый код; настоящий `/clear` в пробной сессии даёт в
журнале плеера `session clear` и `session replace`.

**Files:** нет (вне репозитория: процесс плеера).

**Contract:** обслуживает контракт Task 1; своего артефакта нет.

```
перезапуск: kill -TERM $(cat $XDG_RUNTIME_DIR/tts-player/player.lock)
            setsid tts-player --tray < /dev/null >> $XDG_RUNTIME_DIR/tts-player/player.log 2>&1 &
проба:      (sleep 12; printf '/clear\r'; sleep 8; printf '/exit\r'; sleep 6) \
              | timeout 40 script -qfc claude /dev/null > /dev/null
            пробная сессия не активна (активна текущая) → ждём «session clear», без «session replace»
```

**Acceptance Criteria:**
- [ ] после перезапуска `ps -o lstart` процесса плеера — позже слияния; `tts-player session has
      <UUID текущей сессии>` → 0 (список пережил перезапуск)
- [ ] пробная сессия с `/clear`: в журнале плеера строки `session clear <uuid1>`, затем `session add
      <uuid2>`, затем при выходе `session remove <uuid2>`

**Verify:** `grep -n "session \(add\|clear\|replace\|remove\)" $XDG_RUNTIME_DIR/tts-player/player.log | tail`
→ строки пробы в порядке критериев

**Steps:**

- [ ] **Step 1: Перезапуск плеера** — по контракту, после слияния.
- [ ] **Step 2: Проба** — пробная сессия `claude` через `script` с `/clear` и `/exit`.
- [ ] **Step 3: Commit** — правок в репозитории нет; в отчёте — строки журнала.
