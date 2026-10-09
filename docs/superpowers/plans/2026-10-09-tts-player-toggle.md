# TTS Player — `/tts-on` и `/tts-off`: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/tts-on` запускает демон с обёрткой в `PATH` и плеер в трее, `/tts-off`
останавливает звук плеера; плеер — один экземпляр.

**Architecture:** исполняемый `bin/tts-player` в репозитории (ссылка из `~/.local/bin`,
как у `claude-tts` и `piper`) запускает пакет и принимает команду `stop`. Логика `/tts-on`
и `/tts-off` — в скрипте репозитория `bin/tts-player-hook`; живой хук
`~/.claude/hooks/tts-toggle.sh` (не под git) получает по одной строке вызова в каждой
ветке.

**Tech Stack:** bash + coreutils, `flock` (util-linux), Python 3.12 + системный PyGObject — как в `master`. Новых пакетов нет.

**Slice:** контракт (4) «`tts-toggle.sh` → плеер» — последний на карте; пишущая сторона — хук, читающая — плеер.

**Next slices (not planned here):** нет — карта закрыта.

**Spec:** `docs/superpowers/specs/2026-10-09-tts-player-design.md`

**Assumptions:**
- Демон стартует двойным `fork` от вызвавшего и наследует его окружение; перезапуск по
  управляющему сообщению — `os.execv` с тем же окружением (`daemon.py:963–1004`); службы
  systemd нет (`systemctl --user list-unit-files` пуст по tts). Проверено 2026-10-09 по коду и системе.
- PID демона — `~/.claude-tts/daemon.pid`; окружение процесса читается из `/proc/<pid>/environ`.
- `claude-tts daemon start` при живом демоне ничего не делает (`start_daemon`, «already running»);
  `claude-tts daemon restart` — stop + start из окружения вызвавшего.
- Хук `UserPromptSubmit` запускается из процесса Claude Code, запущенного в X-сессии: `DISPLAY` есть.
- `~/.claude/hooks` не под git: правка `tts-toggle.sh` необратима без копии — копия кладётся рядом перед правкой.

## Global Constraints

- Контракт (4) из спеки — дословно (`tts-player --tray`, `tts-player stop`, exit-коды, `flock` на `$XDG_RUNTIME_DIR/tts-player/player.lock`).
- Блокировка единственности берётся до `player.start()`: `Inbox.start()` чистит `store/`, второй экземпляр стёр бы файлы первого.
- Тесты не трогают живой флаг `/tmp/claude-tts.on`, живой демон, `~/.claude-tts/`, настоящий `$XDG_RUNTIME_DIR`, экран пользователя; `tts-toggle.sh` тестами не запускается.
- Коммит по путям, без `Co-Authored-By`. Правка вне репозитория — только в Task 3 и только перечисленные файлы.

**User decisions (already made):**
- `/tts-on` рядом с запуском демона проверяет плеер и запускает его свёрнутым в трей.
- `/tts-off` останавливает текущее сообщение, буфер цел, плеер остаётся в трее.
- Обёртке — свой каталог (`daemon-bin/`), не `~/.claude/hooks/bin/`.
- Плеер — один процесс; выход — пункт «Выход» в трее.

## Файлы

| Файл | Кто вызывает | Назначение |
|---|---|---|
| `bin/tts-player` | `tts-player-hook`, пользователь | запуск плеера и команда `stop` |
| `tts_player/__main__.py`, `tts_player/instance.py` | `bin/tts-player` | `--tray`, `stop`, единственность |
| `bin/tts-player-hook` | `~/.claude/hooks/tts-toggle.sh` | логика `on` / `off` |
| `~/.local/bin/tts-player` → `bin/tts-player` | `tts-player-hook` по `PATH` | установка (Task 3) |

---

### Task 1: Единственный экземпляр, `--tray` и `stop`

**Goal:** плеер держит блокировку единственности и принимает команду `stop` от второго вызова.

**Files:**
- Create: `bin/tts-player`, `tts_player/instance.py`
- Modify: `tts_player/__main__.py`, `tts_player/player.py` (только подключение сигнала к `stop_playback`)
- Test: `tests/test_instance.py`, `tests/test_player_e2e.py` (новые случаи)

**Contract:** контракт (4), сторона плеера

```
bin/tts-player [--tray] [--no-gui] [--show] [--audio-sink NAME] [--history-dir DIR] [--history-db FILE]
bin/tts-player stop

блокировка: flock -n на $XDG_RUNTIME_DIR/tts-player/player.lock, держит процесс плеера до выхода;
            в файле — PID процесса
запуск, блокировка занята      → stderr «tts-player уже запущен (PID N)», exit 0, store/ не тронут
запуск, нет графической сессии → exit 1, сообщение в stderr (без --no-gui)
--tray                          → окно скрыто, значок в трее (то же, что запуск без --show)
stop, блокировка свободна      → exit 0, ничего не делает
stop, блокировка занята        → SIGUSR1 процессу из файла → Player.stop_playback(); exit 0
плеер на SIGUSR1                → stop_playback(), строка stderr «stop requested»
```

**Acceptance Criteria:**
- [ ] второй запуск при работающем первом → exit 0, сообщение с PID, файлы `store/` первого на месте
- [ ] блокировка берётся до `player.start()` (второй экземпляр не чистит `store/`)
- [ ] `stop` без запущенного плеера → exit 0, без ошибок
- [ ] `stop` при игре → у плеера строка `stop requested`, дальше нет строк `playing`, пока не придёт `play`/`next`
- [ ] после выхода первого блокировка свободна, новый запуск проходит
- [ ] `bin/tts-player` работает из любого каталога (пакет находится от расположения скрипта, ссылка разыменовывается)

**Verify:** `python3 -m unittest discover -s tests -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — процессы с `--no-gui --audio-sink fakesink` и временным `XDG_RUNTIME_DIR`.
- [ ] **Step 2: Реализация** — по контракту; `GLib.unix_signal_add(SIGUSR1)` в обоих главных циклах.
- [ ] **Step 3: Verify** — команда выше, вывод — в отчёт.
- [ ] **Step 4: Commit** — `bin/tts-player tts_player/instance.py tts_player/__main__.py tts_player/player.py tests/test_instance.py tests/test_player_e2e.py`.

### Task 2: Логика `/tts-on` и `/tts-off`

**Goal:** `bin/tts-player-hook on` поднимает демон с обёрткой в `PATH` и плеер в трее,
`off` останавливает звук плеера.

**Files:**
- Create: `bin/tts-player-hook`
- Test: `tests/test_player_hook.py`

**Contract:** вызов хука

```
bin/tts-player-hook on
  daemon-bin = <репозиторий>/daemon-bin (от расположения скрипта, ссылка разыменовывается)
  демон не работает (нет живого PID в ~/.claude-tts/daemon.pid)
        → PATH="$daemon_bin:$PATH" claude-tts daemon start
  демон работает, в /proc/<pid>/environ PATH начинается не с daemon-bin
        → PATH="$daemon_bin:$PATH" claude-tts daemon restart
  демон работает с daemon-bin первым → не трогать
  затем: setsid tts-player --tray, вывод — в $XDG_RUNTIME_DIR/tts-player/player.log, отвязан от хука
  stdout: одна строка о демоне («демон запущен» / «демон перезапущен с обёрткой» / «демон уже с обёрткой»)
          и одна о плеере («плеер запущен» / «плеер уже запущен»)
bin/tts-player-hook off
  → tts-player stop; stdout: «звук плеера остановлен» / «плеер не запущен»
exit: 0; 1 — нет claude-tts или tts-player, сообщение в stderr

переопределения только для тестов: TTS_HOOK_PROC (вместо /proc), TTS_HOOK_HOME (вместо $HOME)
```

**Acceptance Criteria:**
- [ ] демона нет → вызвана `claude-tts daemon start` с `daemon-bin` первым в `PATH`
- [ ] демон с чужим `PATH` → `daemon restart` с `daemon-bin` первым
- [ ] демон с `daemon-bin` первым → ни `start`, ни `restart`
- [ ] плеер запускается с `--tray`, хук возвращается сразу (не ждёт плеер)
- [ ] `off` вызывает `tts-player stop`
- [ ] нет `claude-tts` в `PATH` → exit 1, сообщение
- [ ] тест не трогает `/tmp/claude-tts.on`, `~/.claude-tts/` и живой демон (заглушки `claude-tts`/`tts-player` в `PATH`, временные `TTS_HOOK_HOME`/`TTS_HOOK_PROC`)

**Verify:** `python3 -m unittest tests.test_player_hook -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — заглушки пишут свои аргументы и `PATH` в файл.
- [ ] **Step 2: Реализация** — по контракту; стиль шапки — как у `daemon-bin/paplay`.
- [ ] **Step 3: Verify** — команда выше, вывод — в отчёт.
- [ ] **Step 4: Commit** — `bin/tts-player-hook tests/test_player_hook.py`.

### Task 3: Подключение к `tts-toggle.sh` и установка

**Goal:** живой хук зовёт `tts-player-hook`, `tts-player` и `tts-player-hook` доступны по `PATH`.

**Files:**
- Modify (вне репозитория): `~/.claude/hooks/tts-toggle.sh`
- Create (вне репозитория): `~/.local/bin/tts-player` → `<репозиторий>/bin/tts-player`, `~/.local/bin/tts-player-hook` → `<репозиторий>/bin/tts-player-hook`
- Create (вне репозитория): `~/.claude/hooks/tts-toggle.sh.bak-2026-10-09` — копия до правки

**Contract:** правка хука — задача без своего стыка; держится контракта (4) и вызова Task 2

```
on:  блок «if mode == queue: claude-tts daemon start …; claude-tts daemon status | head -1»
     → «if mode == queue: tts-player-hook on»
off: после ветки, где флаг снят (эта сессия выключена), — «tts-player-hook off»
шапка tts-toggle.sh: строка о плеере и tts-player-hook
```

**Acceptance Criteria:**
- [ ] `bash -n ~/.claude/hooks/tts-toggle.sh` без ошибок; диф с копией — только перечисленные строки
- [ ] `readlink -f ~/.local/bin/tts-player` указывает в репозиторий; `command -v tts-player tts-player-hook` находит обе
- [ ] хук и `/tts-on` вживую не запускались (живой флаг и демон) — проверка передана пользователю
- [ ] `README.md` в корне репозитория: что это, установка (ссылки), команды, как проверить

**Verify:** `bash -n ~/.claude/hooks/tts-toggle.sh && diff ~/.claude/hooks/tts-toggle.sh.bak-2026-10-09 ~/.claude/hooks/tts-toggle.sh; command -v tts-player tts-player-hook`

**Steps:**

- [ ] **Step 1: Копия** — `cp -p` в `.bak-2026-10-09`, до любой правки.
- [ ] **Step 2: Правка и ссылки** — только перечисленное.
- [ ] **Step 3: README** — в репозитории.
- [ ] **Step 4: Commit** — `README.md` (файлы вне репозитория в git не попадают).
