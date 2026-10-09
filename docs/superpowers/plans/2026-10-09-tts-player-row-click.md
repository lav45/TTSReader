# TTS Player — клик по строке списка играет сообщение. План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** одиночный клик по строке списка сообщений делает сообщение текущим и играет его с начала.

**Architecture:** `Player.play_message(message)` — новая команда рядом с `next`/`prev`, на тех же
`_leave`/`_begin`. Окно делает строки списка активируемыми и по `row-activated` зовёт команду с
`state.messages[i]` строки.

**Tech Stack:** Python 3.12 + системный PyGObject (GTK3, GStreamer) — как в `master`. Новых зависимостей нет.

**Slice:** контракт (8) «окно → плеер: выбор сообщения кликом» — первым из оставшихся, потому
что это дефект, найденный пользователем при проверке.

**Next slices (not planned here):** контракт (7) «плеер → хук `/tts-off`» (кнопка [stop]);
контракт (6) «плеер ↔ его файл настроек» (размер хвоста).

**Spec:** `docs/superpowers/specs/2026-10-09-tts-player-settings-design.md`

**Assumptions:**
- `Gtk.ListBox` с `activate-on-single-click=True` шлёт `row-activated` на одиночный клик при любом
  `selection-mode`, включая `NONE` (документация GTK 3.10+); не риск — проверяется GUI-случаем.
- `next`/`prev` уже играют новое сообщение с начала из любого статуса (`Player._begin` ставит
  `playing`) — проверено по `tts_player/player.py:153–167, 291–295`.

## Global Constraints

- Контракт (8) из спеки — дословно.
- Окно отдаёт команды `Player` только через его методы; что показывать — решает `tts_player/view.py`.
- GUI-тесты — через `broadwayd` (`tests/test_window_gui.py`), экран пользователя не трогается.
- Коммит по путям, без `Co-Authored-By`.

**User decisions (already made):**
- Одиночный клик — сообщение становится текущим и сразу играет с начала, из игры, паузы и стопа.

## Файлы

| Файл | Кто вызывает | Назначение |
|---|---|---|
| `tts_player/player.py` | `window.py` | команда `play_message` |
| `tts_player/window.py` | `__main__.py` | активируемые строки списка |
| `tests/test_player.py` | `unittest discover` | команда на заглушке воспроизведения |
| `tests/gui_window_cases.py` | `tests/test_window_gui.py` | клик по строке на `FakePlayer` |

---

### Task 1: Команда `Player.play_message`

**Goal:** плеер делает переданное сообщение текущим и играет его с начала.

**Files:**
- Modify: `tts_player/player.py`
- Test: `tests/test_player.py`

**Contract:** контракт (8), сторона плеера

```
Player.play_message(message: Message) -> None
  message нет в очереди (вытеснено / очищено к моменту клика) → ничего не меняется
  покидаемое текущее → прослушано, только если синтезировано целиком (то же правило, что next/prev)
  message → текущее, позиция 0, статус playing — из playing, paused и stopped
  message == текущее → ничего не меняется
  on_change вызывается
```

**Acceptance Criteria:**
- [ ] из `playing`, `paused`, `stopped`: после `play_message(m)` текущее — `m`, статус `playing`, воспроизведение начато с позиции 0
- [ ] покинутое завершённое сообщение отмечено прослушанным, растущее — нет
- [ ] `play_message(текущее)` ничего не меняет (статус, позиция, воспроизведение)
- [ ] сообщение, которого нет в `queue.messages` (после `clear`), — состояние и воспроизведение не меняются
- [ ] из `idle` без текущего — `m` становится текущим и играет

**Verify:** `python3 -m unittest tests.test_player -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — на той же заглушке воспроизведения, что существующие тесты `next`/`prev`.
- [ ] **Step 2: Реализация** — по контракту, на `_leave`/`_begin`; логику next/prev не дублировать.
- [ ] **Step 3: Verify** — команда выше и полный `python3 -m unittest discover -s tests`.
- [ ] **Step 4: Commit** — `tts_player/player.py tests/test_player.py`.

### Task 2: Строки списка активируются кликом

**Goal:** одиночный клик по строке зовёт `player.play_message` с сообщением этой строки.

**Files:**
- Modify: `tts_player/window.py`
- Test: `tests/gui_window_cases.py`

**Contract:** контракт (8), сторона окна

```
message_list: activate-on-single-click = True; строки активируемые
row-activated(row) → player.play_message(state.messages[row.get_index()])
  state — снимок, по которому построены строки (тот, что показан)
FakePlayer.play_message(message) записывает ("play_message", message)
```

**Acceptance Criteria:**
- [ ] активация второй строки вызывает `play_message` с `state.messages[1]` (тот же объект)
- [ ] после перестройки списка (новый снимок с другим порядком) активация строки берёт сообщение из нового снимка
- [ ] существующие GUI-случаи проходят

**Verify:** `python3 -m unittest discover -s tests` → `OK`, GUI-случаи не пропущены; `pgrep broadwayd` пуст

**Steps:**

- [ ] **Step 1: Падающие GUI-случаи** — активация строки через `row.activate()` или `message_list.emit("row-activated", row)`.
- [ ] **Step 2: Реализация** — окно держит снимок сообщений, по которому построены строки.
- [ ] **Step 3: Verify** — команда выше, вывод — в отчёт.
- [ ] **Step 4: Commit** — `tts_player/window.py tests/gui_window_cases.py`.
