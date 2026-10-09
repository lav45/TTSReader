# TTS Player — команда сессии `add`/`remove` через control/. План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `tts-player session add <uuid> <cwd>` и `session remove <uuid>` кладут запрос в
`$XDG_RUNTIME_DIR/tts-player/control/`, плеер забирает его и меняет список сессий
(`sessions.json`).

**Architecture:** пишущая сторона — подкоманды `session add`/`remove` в `tts_player/__main__.py`
(рядом с `session has` прошлого среза): сверяются с `sessions.json` через `read_sessions` и
атомарно кладут JSON-запрос. Читающая — новый `tts_player/control.py` (`ControlRequests`): монитор
каталога, как `hook_tasks.py`, отдаёт запросы плееру; `Player.add_session`/`remove_session`
меняют реестр `Sessions` (прошлый срез), который `__main__` загружает при старте и передаёт
плееру. Сообщения по сессиям и удаление их файлов при `remove` — срез «сессии внутри плеера».

**Tech Stack:** Python 3.12 + системный PyGObject (Gio, GLib) — как в `master`. Новых зависимостей нет.

**Slice:** контракт (14) «CLI → плеер: команда сессии» — вторым: стоит на `sessions.json` (15),
уже влитом; без него хукам нечем регистрировать сессию.

**Next slices (not planned here):** переключение хуков (`tts-player-hook on <uuid> <cwd>`, гейт,
`tts-toggle.sh`, `tts-command.sh`); сессии внутри плеера; окно.

**Spec:** `docs/superpowers/specs/2026-10-09-tts-player-sessions-design.md`

**Assumptions:**
- `Gio.FileMonitor` с `WATCH_MOVES` ловит rename `.tmp` → `.json` — так уже работают
  `tts_player/hook_tasks.py` и `inbox.py` — не риск.
- Реестр `Sessions`, `read_sessions`, `sessions_path` и `session has` — прошлый срез (15), в `master`.
- Процесс плеера с `--no-gui` во временном окружении — приём `tests/test_player_e2e.py`
  (`PlayerProcessCase`) — не риск.

## Global Constraints

- Контракт (14) дословно (см. Task 1 и Task 2); формат `sessions.json` (15) не меняется.
- Запрос при незапущенном плеере всё равно кладётся: его заберёт старт плеера; stdout говорит
  об этом (решение при планировании, записано в спеке).
- Первая сессия в пустом списке активна; удалённая активная оставляет `active: null` — это
  уже делает `Sessions` прошлого среза, плеер его не обходит.
- Плеер забирает `control/` раньше задач хука на старте (`Player.start`).
- Тесты не трогают настоящий `$XDG_RUNTIME_DIR` и `~/.claude-tts`.
- Коммит по путям, без `Co-Authored-By`; без `git push`.

**User decisions (already made):**
- `/tts-on` и `/tts-off` только регистрируют и снимают сессию; воспроизведение не трогают.
- `/tts-on` в сессии, которая уже в списке, ничего не меняет; ответ «сессия уже в списке».

## Файлы

| Файл | Кто создаёт / кто вызывает | Назначение |
|---|---|---|
| `tts_player/__main__.py` | `bin/tts-player` | `session add`/`remove`; `Sessions` в плеер |
| `tts_player/control.py` | создаёт `player.py` | забор запросов `control/` |
| `tts_player/player.py` | `__main__.py` | `add_session`/`remove_session`, старт монитора |
| `tests/test_session_command.py`, `tests/test_control.py`, `tests/test_player_e2e.py` | `unittest discover` | CLI; монитор; стык через процесс |
| `README.md` | читатель | раздел о списке сессий: `add`/`remove`, `control/` |

---

### Task 1: CLI кладёт запросы add и remove в control/

**Goal:** `session add`/`remove` отвечают по `sessions.json` и атомарно пишут запрос в `control/`.

**Files:**
- Modify: `tts_player/__main__.py`
- Test: `tests/test_session_command.py`

**Contract:** CLI, пишущая сторона (14)

```
tts-player session add <uuid> <cwd> | remove <uuid> | has <uuid>
  add:    уже в списке → stdout «сессия уже в списке», exit 0; иначе запрос в
          $XDG_RUNTIME_DIR/tts-player/control/<ns>.json {"op":"add","session":…,"cwd":…}
          (сначала .tmp, затем rename)
  remove: запрос {"op":"remove","session":…}; нет в списке → «сессия не в списке», exit 0
  has:    exit 0 — в списке, 1 — нет (читает sessions.json, плеер не трогает)
  плеер не запущен → add/remove: exit 0 с сообщением; has → 1

<ns> — time.time_ns() записи; временный файл — control/<ns>.json.tmp
stdout add:    «сессия передана плееру» | «сессия уже в списке»
               | «плеер не запущен — сессия добавится при его запуске»
stdout remove: «сессия передана плееру на удаление» | «сессия не в списке»
               | «плеер не запущен — сессия уберётся при его запуске»
нет XDG_RUNTIME_DIR → stderr, exit 1; сбой записи запроса → stderr, exit 1
неверные аргументы → usage в stderr, exit 2
```

**Acceptance Criteria:**
- [ ] `add u1 /p/TTSReader` при пустом списке и запущенном плеере (блокировка взята в тесте) → в `control/` ровно один `*.json` с `{"op": "add", "session": "u1", "cwd": "/p/TTSReader"}`, stdout «сессия передана плееру», exit 0
- [ ] `add u1 …`, когда `u1` уже в `sessions.json` → «сессия уже в списке», `control/` пуст, exit 0
- [ ] `remove u1` при `u1` в списке → запрос `{"op": "remove", "session": "u1"}`; `remove u2` (нет в списке) → «сессия не в списке», запроса нет
- [ ] плеер не запущен → `add` всё равно кладёт запрос, stdout «плеер не запущен — сессия добавится при его запуске», exit 0; то же для `remove`
- [ ] после команды в `control/` нет `*.tmp`
- [ ] `control/` нет — создаётся командой
- [ ] `session add u1` (без cwd), `session remove` → exit 2 и usage
- [ ] `has` прошлого среза ведёт себя по-прежнему (его тесты зелёные)

**Verify:** `python3 -m unittest tests.test_session_command -v` → `OK`

**Steps:**

- [ ] **Step 1: Failing acceptance** — `bin/tts-player session add u1 /p/TTSReader` подпроцессом во временном runtime с взятой блокировкой; ждём файл запроса. Падает: подкоманды нет (exit 2).
- [ ] **Step 2: Реализовать add/remove** — сверка со списком через `read_sessions` (битый файл — как пустой список); запись через `.tmp` + `os.replace`; ветка `has` не меняется.
- [ ] **Step 3: Остальные критерии** — тест на критерий, каждый сначала красный.
- [ ] **Step 4: Verify** — команда выше, затем весь набор `python3 -m unittest discover -s tests`.
- [ ] **Step 5: Commit** — `tts_player/__main__.py tests/test_session_command.py`.

---

### Task 2: Плеер забирает запросы control/ и меняет список сессий

**Goal:** плеер следит за `control/`, выполняет `add`/`remove` над `Sessions` и удаляет файл;
реестр загружается при старте плеера.

**Files:**
- Create: `tts_player/control.py`
- Modify: `tts_player/player.py`, `tts_player/__main__.py`
- Test: `tests/test_control.py`, `tests/test_player.py`

**Contract:** читающая сторона (14) и вход плеера, которым её зовут

```
плеер: забирает control/*.json монитором (как inbox/), выполняет, удаляет файл;
       не JSON / неизвестный op → файл удалён, строка в stderr

{"op": "add",    "session": "<uuid>", "cwd": "<путь>"}
{"op": "remove", "session": "<uuid>"}

class ControlRequests:
    def __init__(self, control_dir: Path,
                 on_add: Callable[[str, str], None],      # (session, cwd)
                 on_remove: Callable[[str], None]) -> None  # (session)
    def start(self) -> None       # mkdir, монитор, затем скан по порядку имён
    def stop(self) -> None
    def drain(self) -> None       # забрать всё, что лежит сейчас, по порядку имён

class Player:
    def __init__(..., sessions: Sessions, control_dir: Path | None = None)  # None — без control/
    def add_session(self, session_id: str, cwd: str) -> None
    def remove_session(self, session_id: str) -> None

журнал плеера (stderr): «session add <uuid>», «session remove <uuid>»
```

**Acceptance Criteria:**
- [ ] файл `{"op":"add","session":"u1","cwd":"/p/TTSReader"}` в `control/` после `start()` → `on_add("u1", "/p/TTSReader")`, файл удалён
- [ ] файлы, лежавшие до `start()`, забираются по порядку имён (add, затем remove той же сессии → сессии нет)
- [ ] не JSON; не объект; неизвестный `op`; нет строкового `session`; `add` без строкового `cwd` → файл удалён, строка WARNING, колбэк не зван
- [ ] `*.tmp` и имена с точкой в начале не забираются
- [ ] `drain()` забирает лежащие файлы синхронно, без цикла GLib
- [ ] `Player.add_session("u1", "/p/TTSReader")` → `sessions.json` с `u1` активной и строка «session add u1»; `remove_session` → нет `u1`, строка «session remove u1»
- [ ] `Player.start()` забирает `control/` раньше задач хука; `shutdown()` останавливает монитор
- [ ] `__main__` загружает `sessions.json` прошлого запуска до `player.start()` (сессии переживают перезапуск)

**Verify:** `python3 -m unittest tests.test_control tests.test_player -v` → `OK`

**Steps:**

- [ ] **Step 1: Failing acceptance** — `ControlRequests` над временным каталогом: файл `add` до `start()`, после `start()` колбэк зван. Падает: модуля нет.
- [ ] **Step 2: Реализовать `ControlRequests`** — по образцу `hook_tasks.py` (монитор до скана, повторный забор того же имени безвреден, упавший колбэк не оставляет файл).
- [ ] **Step 3: Подключить к плееру** — `Player` получает `sessions` и `control_dir`; `add_session`/`remove_session` — над `Sessions`, со строкой журнала; `start()` — `control` раньше `hook_tasks`; `__main__` строит `Sessions(sessions_path(runtime))`, `load()`, передаёт плееру и `control_dir = runtime/tts-player/control`.
- [ ] **Step 4: Verify** — команда выше, затем весь набор.
- [ ] **Step 5: Commit** — `tts_player/control.py tts_player/player.py tts_player/__main__.py tests/test_control.py tests/test_player.py`.

---

### Task 3: Приёмка через стык — CLI и процесс плеера

**Goal:** настоящий процесс плеера (`--no-gui`) и `bin/tts-player session …` во временном
окружении проходят цикл add → has → remove; README описывает команду сессии.

**Files:**
- Modify: `tests/test_player_e2e.py`, `README.md`

**Contract:** стык (14) целиком — тот же, что в Task 1 и Task 2; своего артефакта нет.

**Acceptance Criteria:**
- [ ] плеер запущен; `session add u1 /p/TTSReader` → в stderr плеера «session add u1»; затем `session has u1` → exit 0; `sessions.json` — `{"active": "u1", "sessions": [{"id": "u1", "project": "TTSReader"}]}`
- [ ] повторный `session add u1 …` → «сессия уже в списке»
- [ ] `session remove u1` → «session remove u1», затем `has u1` → exit 1, `active: null`
- [ ] плеер не запущен: `session add u2 /p/X` → запрос лежит; старт плеера → «session add u2», `has u2` → 0
- [ ] сессия переживает перезапуск: SIGTERM, новый старт → `has u1` → 0 без нового `add`
- [ ] README: раздел о списке сессий описывает `add`/`remove`/`has`, `control/`, `sessions.json` и что запрос при незапущенном плеере выполнится на старте

**Verify:** `python3 -m unittest tests.test_player_e2e -v` → `OK`

**Steps:**

- [ ] **Step 1: Failing acceptance** — e2e: процесс плеера по образцу `PlayerProcessCase`, `session add` подпроцессом, ждём строку журнала.
- [ ] **Step 2: Остальные критерии** — по тесту на критерий; правки кода — только если тест вскрыл дефект прошлых задач (назвать его в отчёте).
- [ ] **Step 3: README** — дописать раздел прошлого среза.
- [ ] **Step 4: Verify** — команда выше, затем весь набор.
- [ ] **Step 5: Commit** — `tests/test_player_e2e.py README.md` (и исправленные файлы, если были).
