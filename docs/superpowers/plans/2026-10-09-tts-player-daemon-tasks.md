# TTS Player — очередь демона пишет только плеер; переозвучка. План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** задачи хука идут в демон только через плеер; плеер знает текст, голос и скорость каждого
сообщения и при проигрывании сообщения с устаревшим кэшем ставит его в демон заново.

**Architecture:** `tts_player/daemon_queue.py` пишет задачу в `~/.claude-tts/queue/` с новым id и
`"source": "tts-player"`. Плеер на задачу хука заводит запись сообщения (текст, persona, speed,
сессия) и связывает её с id задачи демона; предложения с этим id (контракт (13)) попадают в
сообщение, чужие и `unknown` — удаляются. Устаревший кэш (persona/speed ≠ текущим из
`config.json`) при начале проигрывания сбрасывается, и в демон уходит задача переозвучки с новым id
того же сообщения. Скрипты хуков `speak-response.sh` и `speak-intermediate.sh` получают
`HOME=$XDG_RUNTIME_DIR/tts-player/hook-home`.

**Tech Stack:** Python 3.12 + системный PyGObject — как в `master`; bash. Новых зависимостей нет.

**Slice:** контракт (12) «плеер → демон: задача демону» — последний в спеке кэша; включает
переключение хуков, потому что без него (11) и (12) в работе не видны.

**Next slices (not planned here):** спека сессий — (14) команды сессии, (15) `sessions.json`.

**Spec:** `docs/superpowers/specs/2026-10-09-tts-player-text-cache-design.md`

**Assumptions:**
- Демон пишет `current_message.id` только для задач с `source` (`daemon.py:1447, 1499–1501`) —
  проверено ревью среза (13); задача плеера несёт `"source": "tts-player"`.
- `JOBS.update` демона для id, которого нет в реестре моста, ничего не ломает: проверить чтением
  `claude_code_tts/bridge.py` (класс реестра) в Task 1, Step 1; если ломает — остановить план.
- Хук с `HOME=hook-home` пишет задачу в hook-home и говорит только при свежем heartbeat
  hook-home — проверено пробой и срезом (11).
- Хуки Claude Code: `tts-gate.sh` зовёт `speak-response.sh` и `speak-intermediate.sh`;
  `tts-command.sh` при `/tts-on` зовёт `speak-response.sh` напрямую — поэтому HOME ставится в самих
  скриптах `speak-*.sh`, а не в гейте (моё решение: одно место на оба пути вызова).

## Global Constraints

- Контракт (12) из спеки — дословно (поля, `source`, новый id, атомарная запись, переозвучка по
  текущей персоне).
- Контракт (13) включается полностью: предложение с `unknown` или неизвестным плееру id —
  файл удалён, строка в stderr.
- Тесты не трогают настоящий `~/.claude-tts`, `$XDG_RUNTIME_DIR`, демон, `claude-tts`, `pactl`,
  экран; хуки `~/.claude/hooks/*` тестами не запускаются.
- Правка `~/.claude/hooks/speak-response.sh` и `speak-intermediate.sh` — только в Task 3, с копиями
  `*.bak-2026-10-09` до правки.
- Сбой обработки задачи хука не теряет задачу молча: строка в stderr с id и текстом задачи урезанным
  до 80 знаков.
- Коммит по путям, без `Co-Authored-By`.
- `master` — живая установка: отчёт о слиянии говорит, что плеер нужно перезапустить (новый формат
  обработки), и в каком порядке включаются хуки.

**User decisions (already made):**
- В `~/.claude-tts/queue/` задачи кладёт только плеер; демон под управлением плеера.
- Переозвучка — очередью демона; при проигрывании сообщения с устаревшим кэшем.
- Текст и кэш — временные.

## Файлы

| Файл | Кто вызывает | Назначение |
|---|---|---|
| `tts_player/daemon_queue.py` | `player.py` | запись задачи демону |
| `tts_player/model.py`, `queue.py`, `player.py` | — | текст и параметры сообщения, связь id, переозвучка |
| `~/.claude/hooks/speak-response.sh`, `speak-intermediate.sh` (вне репозитория) | хуки Claude Code | HOME = hook-home |
| `README.md` | — | устройство, перезапуск, откат хуков |

---

### Task 1: Запись задачи демону

**Goal:** `DaemonQueue.put(fields) -> str` атомарно пишет задачу в очередь демона с новым id и `source`.

**Files:**
- Create: `tts_player/daemon_queue.py`
- Test: `tests/test_daemon_queue.py`

**Contract:** контракт (12), запись

```
class DaemonQueue:
    SOURCE = "tts-player"
    def __init__(self, queue_dir: Path, clock=time.time, new_id=lambda: secrets.token_hex(8)) -> None
    def put(self, fields: dict) -> str     # возвращает id; raises OSError наружу не выпускает → None
файл:  queue_dir/<time:.6f>_<id>.json, сначала .<имя>.tmp в том же каталоге, затем rename
поля:  fields как есть, затем id = новый, timestamp = clock(), source = SOURCE
ошибка записи → строка в stderr, put возвращает None
```

**Acceptance Criteria:**
- [ ] файл с именем `<ts>_<id>.json`, поля задачи сохранены, `id`/`timestamp`/`source` заменены
- [ ] временного файла после записи нет; демон не видит `.tmp` (имя не `*.json`)
- [ ] каталог только для чтения → `None`, строка в stderr
- [ ] Step 1 проверил по `bridge.py`, что `JOBS.update` для чужого id безопасен (вывод в отчёте)

**Verify:** `python3 -m unittest discover -s tests -p 'test_daemon_queue.py' -v` → `OK`

**Steps:**

- [ ] **Step 1: Проверка JOBS** — чтением `claude_code_tts/bridge.py`; небезопасно → BLOCKED.
- [ ] **Step 2: Падающие тесты** — временный каталог очереди.
- [ ] **Step 3: Реализация** — по контракту.
- [ ] **Step 4: Commit** — `tts_player/daemon_queue.py tests/test_daemon_queue.py`.

### Task 2: Сообщение знает свой текст; переозвучка при проигрывании

**Goal:** задача хука → запись в демон и сообщение с текстом; предложения связываются по id; устаревший кэш переозвучивается при начале проигрывания.

**Files:**
- Modify: `tts_player/model.py`, `tts_player/queue.py`, `tts_player/player.py`, `tts_player/__main__.py`
- Test: `tests/test_queue.py`, `tests/test_player.py`, `tests/test_player_e2e.py`

**Contract:** контракт (12) и (13), сторона плеера

```
Message += text: str | None, persona: str | None, speed: float | None, task_ids: set[str]
задача хука → DaemonQueue.put(task.fields) = id → сообщение (session_id, project, text, persona,
              speed из задачи), task_ids = {id}; в очереди сообщение есть сразу (без предложений)
предложение с task_id ∈ task_ids сообщения → в это сообщение; unknown / чужой id → файл удалён,
              строка в stderr
put вернул None → сообщение помечено «не отправлено», строка в stderr с id задачи хука
устаревший кэш: persona или speed сообщения ≠ текущим (active_persona и её speed из
              ~/.claude-tts/config.json, читается при начале проигрывания)
начало проигрывания (play/pause, автопереход, next/prev+play) сообщения с устаревшим кэшем или
              без звука → его файлы удалены, предложения забыты, DaemonQueue.put(переозвучка) —
              text сообщения, persona/speed/speed_method/voice_* из текущей персоны config.json,
              session_id/project сообщения; новый id добавлен в task_ids; persona/speed сообщения =
              текущие; сообщение играет по мере прихода предложений (как растущее)
сбой разбора config.json → кэш считается актуальным, строка в stderr
__main__: DaemonQueue(Path("~/.claude-tts/queue").expanduser()); хвост сообщений — как сейчас
```

**Acceptance Criteria:**
- [ ] задача хука → файл в очереди демона с `source` и новым id; в списке сообщение с текстом до прихода звука
- [ ] предложения с этим id попадают в сообщение; `unknown` и чужой id — файл удалён, строка в stderr
- [ ] смена `active_persona` в подставном `config.json` → play этого сообщения кладёт задачу переозвучки (новый id, текущая персона), старые файлы удалены, новые предложения играют
- [ ] кэш актуален → play без новой задачи
- [ ] сбой записи задачи → строка в stderr, плеер не падает
- [ ] e2e: подставная задача хука в hook-home → файл в подставной очереди демона с `source: tts-player`; файлы с этим id через настоящую обёртку (playback.json подставной) → `playing`

**Verify:** `python3 -m unittest discover -s tests` → `OK`, GUI-случаи не пропущены

**Steps:**

- [ ] **Step 1: Падающие тесты** — очередь, плеер на заглушках, e2e.
- [ ] **Step 2: Реализация** — по контракту; «начало проигрывания» — одно место в плеере, а не в каждой команде.
- [ ] **Step 3: Verify** — команда выше, вывод — в отчёт.
- [ ] **Step 4: Commit** — файлы задачи по путям.

### Task 3: Хуки пишут задачу в плеер; README

**Goal:** `speak-response.sh` и `speak-intermediate.sh` запускают `claude-tts` с HOME = hook-home; README описывает новую цепочку.

**Files:**
- Modify (вне репозитория): `~/.claude/hooks/speak-response.sh`, `~/.claude/hooks/speak-intermediate.sh` — копии `*.bak-2026-10-09` до правки
- Modify: `README.md`

**Contract:** контракт (11), сторона хука

```
speak-*.sh: HOOK_HOME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/tts-player/hook-home"
            HOME="$HOOK_HOME" PATH=… exec claude-tts speak --from-hook …   (остальное как было)
плеер не запущен → нет heartbeat в hook-home → хук молча пропускает речь (поведение claude-tts)
```

**Acceptance Criteria:**
- [ ] `bash -n` обоих скриптов; `diff` с копиями — только строка запуска
- [ ] README: цепочка хук → плеер → демон, перезапуск плеера после обновления, откат хуков из копий
- [ ] вживую хуки не запускались — проверка передана пользователю (что делать: перезапустить плеер, `/tts-on`, ответ звучит; в журнале плеера `task …`)

**Verify:** `bash -n ~/.claude/hooks/speak-response.sh && bash -n ~/.claude/hooks/speak-intermediate.sh && diff ~/.claude/hooks/speak-response.sh.bak-2026-10-09 ~/.claude/hooks/speak-response.sh`

**Steps:**

- [ ] **Step 1: README** — в ветке.
- [ ] **Step 2: Commit** — `README.md`.
- [ ] **Step 3: После слияния, с согласия пользователя** — копии `cp -p`, правка строки запуска, `bash -n`, `diff`; затем пользователь перезапускает плеер. Правка хуков живая: сделанная до перезапуска плеера с кодом (12), она выключит звук.
