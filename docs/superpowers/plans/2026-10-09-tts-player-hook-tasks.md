# TTS Player — задачи хука приходят в плеер. План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** плеер готовит подменённый HOME для хука `claude-tts`, ведёт в нём heartbeat и забирает
задачи хука из его очереди, сохраняя текст и параметры сообщения.

**Architecture:** `tts_player/hook_home.py` создаёт `$XDG_RUNTIME_DIR/tts-player/hook-home/.claude-tts`
(ссылки на настоящие `config.json` и `sessions.d`, своя `queue/`) и раз в 10 с обновляет или
удаляет там `daemon.heartbeat` по здоровью настоящего демона. `tts_player/hook_tasks.py` следит за
`hook-home/.claude-tts/queue/` (Gio.FileMonitor, как `inbox.py`) и отдаёт плееру `HookTask`.
Плеер в этом срезе только хранит задачи (журнал); отправку в демон, связь с сообщением и
переключение гейта делает срез (12).

**Tech Stack:** Python 3.12 + системный PyGObject (Gio, GLib) — как в `master`. Новых зависимостей нет.

**Slice:** контракт (11) «хук → плеер: задача хука» — вторым из спеки кэша: даёт плееру текст
сообщений, на котором строится (12). Гейт `~/.claude/hooks/tts-gate.sh` в этом срезе не меняется:
хук продолжает писать в настоящую очередь, пока (12) не научит плеер передавать задачи демону.

**Next slices (not planned here):** контракт (12) «плеер → демон: задача демону» (очередь пишет
только плеер, `source`, переозвучка при проигрывании, переключение гейта); затем спека сессий
((14), (15)).

**Spec:** `docs/superpowers/specs/2026-10-09-tts-player-text-cache-design.md`

**Assumptions:**
- `claude-tts speak --from-hook` с `HOME=<каталог>`, где лежат `config.json`, `sessions.d/` и свежий
  `daemon.heartbeat`, пишет задачу в `<каталог>/.claude-tts/queue/<ts>_<id>.json` через `.tmp` +
  rename — проверено пробой 2026-10-09 (записано в спеке кэша). Со ссылками вместо копий — то же
  чтение `Path` (не риск: `open` идёт по ссылке).
- `daemon_healthy()` хука: свежий `daemon.heartbeat` (≤ 30 с) или живой PID из `daemon.pid`
  (`audio.py:699–722`) — проверено по коду.
- `Gio.FileMonitor` с `WATCH_MOVES` уже ловит rename в `inbox.py` — тот же приём.

## Global Constraints

- Контракт (11) из спеки — дословно (каталог, ссылки, heartbeat раз в 10 с по здоровью настоящего
  демона, формат задачи, забор `*.json` по порядку имени, битая задача — удалить + строка в stderr).
- Тесты не трогают настоящий `~/.claude-tts`, настоящий `$XDG_RUNTIME_DIR` и не запускают
  настоящий `claude-tts`: корень настроек `claude-tts` и runtime-каталог — временные.
- Гейт и хуки Claude Code (`~/.claude/hooks/*`) не меняются.
- Коммит по путям, без `Co-Authored-By`.

**User decisions (already made):**
- Текст ответа попадает в плеер так: гейт запускает `claude-tts speak --from-hook` с HOME, подменённым
  на каталог плеера; плеер ведёт heartbeat в подменённом каталоге.
- Текст и кэш — временные (`$XDG_RUNTIME_DIR`), после перезагрузки история пуста.

## Файлы

| Файл | Кто создаёт / кто вызывает | Назначение |
|---|---|---|
| `tts_player/hook_home.py` | создаёт `__main__.py` | каталог хука и heartbeat |
| `tts_player/hook_tasks.py` | создаёт `player.py` | забор задач хука, `HookTask` |
| `tts_player/player.py`, `tts_player/__main__.py` | — | подключение, хранение задач |

---

### Task 1: Каталог хука и heartbeat

**Goal:** `HookHome` готовит подменённый HOME и держит в нём heartbeat, пока жив настоящий демон.

**Files:**
- Create: `tts_player/hook_home.py`
- Test: `tests/test_hook_home.py`

**Contract:** контракт (11), каталог и heartbeat

```
class HookHome:
    BEAT_SECONDS = 10
    FRESH_SECONDS = 30
    def __init__(self, runtime_dir: Path, tts_root: Path, clock=time.time) -> None
        # runtime_dir = $XDG_RUNTIME_DIR/tts-player; tts_root = ~/.claude-tts
    home: Path            # runtime_dir/hook-home
    queue_dir: Path       # home/.claude-tts/queue
    def prepare(self) -> None
    def beat(self) -> bool    # один шаг: True — heartbeat записан, False — удалён
    def start(self) -> None   # prepare(); beat(); GLib-таймер BEAT_SECONDS → beat()
    def stop(self) -> None    # таймер снят, heartbeat удалён

prepare: создаёт home/.claude-tts/ и queue/; config.json и sessions.d — символьные ссылки на
         tts_root/config.json и tts_root/sessions.d (существующая ссылка/файл заменяется);
         queue/ очищается от остатков прошлого запуска (*.json, *.tmp) — их хук писал прошлому плееру
beat:    настоящий демон жив ⇔ tts_root/daemon.heartbeat свежий (clock() − значение ≤ FRESH_SECONDS)
         или PID из tts_root/daemon.pid существует (os.kill(pid, 0));
         жив → home/.claude-tts/daemon.heartbeat = str(clock()) атомарно; не жив → файл удалён
ошибки:  OSError → строка в stderr, исключение не выходит
```

**Acceptance Criteria:**
- [ ] `prepare()` на пустом каталоге: ссылки указывают на `tts_root/config.json` и `tts_root/sessions.d`, `queue/` есть; повторный `prepare()` не падает и чистит `queue/`
- [ ] `beat()` при свежем настоящем heartbeat → файл в hook-home со значением `clock()`; при устаревшем и без живого PID → файла нет
- [ ] `beat()` при живом PID (собственный PID теста) и без heartbeat → файл записан
- [ ] `stop()` удаляет heartbeat и снимает таймер
- [ ] ошибка записи (каталог только для чтения) → строка в stderr, без исключения

**Verify:** `python3 -m unittest discover -s tests -p 'test_hook_home.py' -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — временные `runtime_dir` и `tts_root`, подставные часы.
- [ ] **Step 2: Реализация** — по контракту; атомарная запись — `tts_player/atomic_json` или тот же приём для текста.
- [ ] **Step 3: Verify** — команда выше и полный `python3 -m unittest discover -s tests`.
- [ ] **Step 4: Commit** — `tts_player/hook_home.py tests/test_hook_home.py`.

### Task 2: Плеер забирает задачи хука

**Goal:** задачи из `hook-home/.claude-tts/queue/` попадают в плеер как `HookTask` и хранятся; `__main__` поднимает `HookHome`.

**Files:**
- Create: `tts_player/hook_tasks.py`
- Modify: `tts_player/player.py`, `tts_player/__main__.py`
- Test: `tests/test_hook_tasks.py`, `tests/test_player.py`, `tests/test_player_e2e.py`

**Contract:** контракт (11), задача хука

```
@dataclass(frozen=True)
class HookTask:
    id: str; timestamp: float; session_id: str; project: str; text: str
    persona: str; speed: float; fields: dict      # fields — вся задача как есть (для (12))

class HookTasks:
    def __init__(self, queue_dir: Path, on_task: Callable[[HookTask], None]) -> None
    def start(self) -> None   # скан существующих *.json по порядку имени, затем монитор
    def stop(self) -> None

забор:  *.json (не *.tmp, не скрытые) по порядку имени; разбор → on_task(HookTask) → файл удалён
битая:  не JSON / не объект / нет id или text (строки) → файл удалён, строка в stderr
Player(…, hook_tasks_dir: Path | None)   # None — без задач хука (прежние тесты)
  Player.tasks: list[HookTask]            # по порядку прихода, хвост 50 последних
  журнал: «task <id> <session_id> <len(text)> chars»
__main__: HookHome(<runtime>, Path("~/.claude-tts").expanduser()).start() до player.start();
          hook_home.stop() в finally после player.shutdown(); --no-gui тоже
```

**Acceptance Criteria:**
- [ ] файл задачи в `queue/` → `on_task` с полями из файла, файл удалён; два файла → в порядке имён
- [ ] `*.tmp` не забирается; rename `.tmp` → `.json` забирается
- [ ] битый JSON, массив, задача без `text` → файл удалён, строка в stderr, `on_task` не вызван
- [ ] `Player.tasks` хранит последние 50 по порядку прихода
- [ ] e2e: процесс плеера с временными `XDG_RUNTIME_DIR` и HOME (корень `claude-tts` — временный): в `hook-home/.claude-tts/queue/` появляется задача → в stderr строка `task <id> <session> <n> chars`, файл удалён; при свежем подставном `~/.claude-tts/daemon.heartbeat` в hook-home есть `daemon.heartbeat`

**Verify:** `python3 -m unittest discover -s tests` → `OK`, GUI-случаи не пропущены

**Steps:**

- [ ] **Step 1: Падающие тесты** — монитор на временном каталоге, e2e на процессе плеера.
- [ ] **Step 2: Реализация** — по контракту; забор — тем же приёмом, что `inbox.py` (скан + монитор, `WATCH_MOVES`).
- [ ] **Step 3: Verify** — команда выше, вывод — в отчёт.
- [ ] **Step 4: Commit** — файлы задачи по путям.
