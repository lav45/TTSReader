# TTS Player — список сессий в sessions.json и `session has`. План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** реестр сессий плеера, который атомарно пишет `sessions.json`, и команда
`tts-player session has <uuid>`, отвечающая гейту по этому файлу.

**Architecture:** `tts_player/sessions.py` — класс `Sessions`: список сессий в порядке
добавления и активная сессия; каждое изменение пишется в `sessions.json` через
`atomic_json.write_json`; `load()` читает файл прошлого запуска. Читающая сторона — подкоманда
`session has` в `tts_player/__main__.py` (тот же разбор файла, плеер не трогает): exit 0 —
сессия в списке и плеер запущен (проба блокировки `instance.is_locked`), иначе 1. Плеер
подключает реестр в следующем срезе вместе с командами `add`/`remove`.

**Tech Stack:** Python 3.12, стандартная библиотека; новых зависимостей нет.

**Slice:** контракт (15) «плеер → гейт и CLI: список сессий» — первым: от него зависят и `add`
контракта (14) («уже в списке»), и гейт.

**Next slices (not planned here):** (14) команда сессии `add`/`remove` через `control/`;
переключение хуков на сессии; сессии внутри плеера (свой хвост, синтез только активной);
окно (список сессий, кнопки активной сессии).

**Spec:** `docs/superpowers/specs/2026-10-09-tts-player-sessions-design.md`

**Assumptions:**
- Атомарная запись JSON — `tts_player/atomic_json.write_json` (временный файл + `os.replace`),
  уже работает для настроек плеера — не риск.
- Проба «плеер запущен» без записи — `tts_player/instance.is_locked`, уже используется командой
  `stop` — не риск.
- Время старта `python3 -m tts_player` ≈ 0,08 с (замер 2026-10-09) — гейт зовёт `has` на каждом
  хуке озвучки, таймаут самого короткого хука 5 с — не риск.

## Global Constraints

- Контракт (15) дословно: путь `$XDG_RUNTIME_DIR/tts-player/sessions.json`, ключи `active`
  (строка или `null`) и `sessions` (список объектов `id`, `project`), пишет только плеер, атомарно.
- `project` — последний компонент `cwd` сессии (`/home/u/Programs/TTSReader/` → `TTSReader`).
- Первая сессия, добавленная в пустой список, становится активной; удалённая активная
  оставляет `active: null` (спека, решения 2026-10-09).
- `sessions.json` переживает выход плеера; `has` при незапущенном плеере — 1 (решение при
  планировании, записано в спеке).
- Тесты не трогают настоящий `$XDG_RUNTIME_DIR`: runtime-каталог временный.
- Коммит по путям, без `Co-Authored-By`; без `git push`.

**User decisions (already made):**
- ID сессии — UUID сессии Claude Code; строка списка — «проект · первые 8 знаков UUID».
- Первая сессия в пустом списке сразу активна; завершена активная — активной нет, пока
  пользователь не кликнет по сессии.

## Файлы

| Файл | Кто создаёт / кто вызывает | Назначение |
|---|---|---|
| `tts_player/sessions.py` | создаёт `__main__.py` (плеер — со следующего среза), читает `session has` | реестр сессий и `sessions.json` |
| `tts_player/__main__.py` | `bin/tts-player` | подкоманда `session has` |
| `tests/test_sessions.py`, `tests/test_session_command.py` | `unittest discover` | реестр; CLI через файл |
| `README.md` | читатель | раздел о `session has` и `sessions.json` |

---

### Task 1: Реестр сессий пишет sessions.json

**Goal:** `Sessions` ведёт список и активную сессию и после каждого изменения атомарно пишет файл
контракта (15); `load()` поднимает файл прошлого запуска.

**Files:**
- Create: `tts_player/sessions.py`
- Test: `tests/test_sessions.py`

**Contract:** файл (15) и интерфейс реестра, которым пользуются плеер и CLI

```
$XDG_RUNTIME_DIR/tts-player/sessions.json   пишет только плеер, атомарно
  {"active": "<uuid>" | null,
   "sessions": [{"id": "<uuid>", "project": "<последний компонент cwd>"}, …]}

@dataclass(frozen=True)
class Session:
    id: str
    project: str

def sessions_path(runtime_dir: Path) -> Path      # runtime_dir/tts-player/sessions.json

def read_sessions(path: Path) -> tuple[str | None, list[Session]]
    # (active, sessions); нет файла — (None, []); битый — ValueError

class Sessions:
    def __init__(self, path: Path) -> None
    def load(self) -> None                       # нет файла — пусто; битый — пусто + WARNING
    @property sessions -> list[Session]          # копия, в порядке добавления
    @property active -> str | None
    def has(self, session_id: str) -> bool
    def add(self, session_id: str, cwd: str) -> bool     # False — уже в списке, файл не пишется
    def remove(self, session_id: str) -> bool            # False — нет в списке
```

**Acceptance Criteria:**
- [ ] `add("u1", "/home/u/Programs/TTSReader/")` в пустой реестр → файл `{"active": "u1", "sessions": [{"id": "u1", "project": "TTSReader"}]}`
- [ ] вторая `add` → сессия в конце списка, `active` прежний
- [ ] повторная `add` той же сессии → `False`, файл не переписан (mtime и содержимое прежние)
- [ ] `remove` активной → её нет в списке, `active: null`; `remove` неактивной → `active` прежний
- [ ] `remove` отсутствующей → `False`
- [ ] `load()` после записи другим экземпляром → те же `sessions` и `active`
- [ ] `load()` без файла → пусто; с битым файлом (не JSON; `sessions` не список; `active` не из списка) → пусто и строка WARNING
- [ ] сбой записи (каталог только для чтения) → WARNING, состояние в памяти изменено, исключения нет
- [ ] `read_sessions` битого файла → `ValueError`

**Verify:** `python3 -m unittest tests.test_sessions -v` → `OK`

**Steps:**

- [ ] **Step 1: Failing acceptance** — тест: `add` в пустой реестр во временном каталоге, файл равен образцу контракта. Падает, потому что модуля нет.
- [ ] **Step 2: Реализовать `Sessions`** — запись только через `atomic_json.write_json`; формат файла — дословно из контракта; разбор файла один (`read_sessions`), его же зовёт `load`.
- [ ] **Step 3: Остальные критерии** — по одному тесту на критерий, каждый сначала красный.
- [ ] **Step 4: Verify** — команда выше, затем весь набор `python3 -m unittest discover -s tests`.
- [ ] **Step 5: Commit** — `tts_player/sessions.py tests/test_sessions.py`.

---

### Task 2: `tts-player session has` читает sessions.json

**Goal:** гейт спрашивает `tts-player session has <uuid>` и получает 0 только для сессии из списка
запущенного плеера.

**Files:**
- Modify: `tts_player/__main__.py` (разбор `argv` до `argparse` — рядом с `stop`/`play`)
- Modify: `README.md`
- Test: `tests/test_session_command.py`

**Contract:** CLI, читающая сторона (15)

```
tts-player session has <uuid>
  exit 0 — <uuid> в sessions["id"] и плеер запущен (instance.is_locked)
  exit 1 — нет в списке; нет файла; файл битый; плеер не запущен; нет XDG_RUNTIME_DIR
  вывода нет; sessions.json не меняется
tts-player session <что-то другое>   → usage в stderr, exit 2
```

**Acceptance Criteria:**
- [ ] реестр (`Sessions.add`) записал `u1`, блокировка плеера взята (`Instance.acquire` во временном runtime) → `has u1` exit 0
- [ ] `has u2` при том же файле → exit 1
- [ ] файл есть, блокировки нет → `has u1` exit 1
- [ ] нет файла / битый файл → exit 1, файл не создан и не изменён
- [ ] `session`, `session has`, `session foo u1` → exit 2 и usage в stderr
- [ ] README описывает `sessions.json` (кто пишет, кто читает) и `session has`

**Verify:** `python3 -m unittest tests.test_session_command -v` → `OK`

**Steps:**

- [ ] **Step 1: Failing acceptance** — через подпроцесс `bin/tts-player session has u1` с `XDG_RUNTIME_DIR` во временном каталоге, после `Sessions.add` и `Instance.acquire` в тесте. Падает: подкоманды нет.
- [ ] **Step 2: Реализовать подкоманду** — файл разбирается `read_sessions`; запуск плеера — `is_locked`; ветка не создаёт каталогов и файлов.
- [ ] **Step 3: Остальные критерии и README** — тесты по критериям; в README — раздел о списке сессий: путь, формат, кто пишет, `has` и его коды.
- [ ] **Step 4: Verify** — команда выше, затем весь набор.
- [ ] **Step 5: Commit** — `tts_player/__main__.py README.md tests/test_session_command.py`.
