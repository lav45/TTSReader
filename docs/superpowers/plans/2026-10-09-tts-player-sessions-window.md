# TTS Player — окно: список сессий и кнопки активной сессии. План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** главный экран показывает список сессий между рядом служебных кнопок и списком
сообщений; клик по сессии делает её активной; мусорка очищает активную сессию, [stop]
завершает её; флаг озвучки и класс `Voicing` уходят.

**Architecture:** `tts_player/view.py` получает `session_rows(state)` — строки списка сессий
из `PlayerState.sessions`/`active` (прошлый срез). `PlayerWindow` строит второй `Gtk.ListBox`
над списком сообщений; клик зовёт `Player.select_session`; кнопка мусорки — `Player.clear`
(уже очищает активную сессию), кнопка [stop] (бывшая «Выключить озвучку») —
`Player.remove_session(state.active)`. `Voicing`, `--tts-flag`, `--tts-off-hook` теряют
потребителя и удаляются.

**Tech Stack:** Python 3.12 + системный PyGObject (Gtk 3, XApp) — как в `master`. Новых зависимостей нет.

**Slice:** окно — читатель входа плеера «сессии» (`PlayerState.sessions`/`active`,
`select_session`, `clear`, `remove_session`) прошлого среза. Последним: строится на нём.

**Next slices (not planned here):** нет — после этого среза спека сессий реализована.

**Spec:** `docs/superpowers/specs/2026-10-09-tts-player-sessions-design.md`

**Assumptions:**
- GUI-тесты окна идут на бэкенде GTK Broadway (`tests/test_window_gui.py` →
  `tests/gui_window_cases.py`) — приём уже работает — не риск.
- `Gtk.ListBox` с `row-activated` по одному клику — так уже сделан список сообщений — не риск.

## Global Constraints

- Список сессий — на главном экране между рядом служебных кнопок и списком сообщений.
- Строка сессии — «проект · первые 8 знаков UUID» (`TTSReader · 1dd2b064`); проект пуст — `—`
  (`view.NO_PROJECT`); в строке нет кнопок; активная выделена жирным, как текущее сообщение.
- Клик по строке другой сессии — `select_session`; по активной — ничего.
- Мусорка — «Очистить» активной сессии: её тексты и звук удалены, сессия остаётся.
- [stop] — «Завершить» активную сессию: то же, что `/tts-off` из её чата (`remove_session`), без
  подтверждения. Остановить звук — пауза плеера.
- Нет активной сессии — мусорка и [stop] неактивны (клик ничего не делает).
- Каждый видимый элемент имеет решённый клик (CLAUDE.md пользователя): строка сессии, мусорка,
  [stop] — по пунктам выше; пустой список сессий — строк нет, кликать нечего.
- Коммит по путям, без `Co-Authored-By`; без `git push`.

**User decisions (already made):**
- Кнопки ряда служебных кнопок наверху управляют активной сессией; в строках сессий кнопок нет.
- Мусорка — «Очистить» активной сессии; [stop] — «Завершить» активную; прежний смысл [stop]
  («остановить звук») убран.
- Клик по другой сессии: звук стоп, она активна, текущее — первое непрослушанное, ждёт play/pause.

## Файлы

| Файл | Кто создаёт / кто вызывает | Назначение |
|---|---|---|
| `tts_player/view.py` | `window.py` | `SessionRow`, `session_rows`, `session_buttons_enabled` |
| `tts_player/window.py` | `__main__.run_gui` | список сессий, кнопки активной |
| `tts_player/__main__.py` | `bin/tts-player` | без `Voicing`, `--tts-flag`, `--tts-off-hook` |
| `tts_player/voicing.py`, `tests/test_voicing.py` | — | удаляются |
| `tests/test_view.py`, `tests/gui_window_cases.py` | `unittest discover` | |
| `README.md` | читатель | окно: список сессий, кнопки |

---

### Task 1: Строки списка сессий в модели отображения

**Goal:** `view.session_rows(state)` даёт строки списка сессий и признак доступности кнопок
активной сессии.

**Files:**
- Modify: `tts_player/view.py`
- Test: `tests/test_view.py`

**Contract:** модель отображения, которую читает окно

```
@dataclass(frozen=True)
class SessionRow:
    title: str      # "<project> · <id[:8]>"; project пуст — NO_PROJECT
    active: bool

def session_rows(state: PlayerState) -> list[SessionRow]     # в порядке state.sessions
def session_buttons_enabled(state: PlayerState) -> bool      # state.active is not None

rows(state): MessageRow.duration для m.deferred — с PENDING_MARK (текст ещё не отдан демону)
```

**Acceptance Criteria:**
- [ ] сессии `Session("1dd2b064-b8fd-…", "TTSReader")` и `Session("abcdef12-…", "")`, активна первая → `[SessionRow("TTSReader · 1dd2b064", True), SessionRow("— · abcdef12", False)]`
- [ ] `active None` → все `active False`; `session_buttons_enabled` → `False`
- [ ] пустой список → `[]`
- [ ] строка сообщения с `deferred=True` показывает длительность с меткой ожидания (`0:00…`), а не как завершённое тихое сообщение (`rows`)

**Verify:** `python3 -m unittest tests.test_view -v` → `OK`

**Steps:**

- [ ] **Step 1: Failing acceptance** — критерий 1. Падает: функции нет.
- [ ] **Step 2: Реализовать** — по контракту; `NO_PROJECT` — существующая константа модуля.
- [ ] **Step 3: Verify** — команда выше, затем весь набор `python3 -m unittest discover -s tests`.
- [ ] **Step 4: Commit** — `tts_player/view.py tests/test_view.py`.

---

### Task 2: Окно — список сессий, мусорка и [stop] активной сессии

**Goal:** окно показывает список сессий и управляет активной сессией; `Voicing` удалён.

**Files:**
- Modify: `tts_player/window.py`, `tts_player/__main__.py`, `README.md`
- Delete: `tts_player/voicing.py`, `tests/test_voicing.py`
- Test: `tests/gui_window_cases.py`

**Contract:** что окно зовёт у плеера и чем отвечает на клик

```
PlayerWindow(player, settings, prefs, text_speaker)        # без voicing
  session_list: Gtk.ListBox      между рядом служебных кнопок и списком сообщений;
                                 строки — view.session_rows(state); активная жирная
  row-activated(i)    → player.select_session(state.sessions[i].id)   (активная — ничего; это решает плеер)
  clear_button        → player.clear()                     tooltip «Очистить сообщения активной сессии»
  end_session_button  → player.remove_session(state.active) tooltip «Завершить активную сессию (/tts-off)»
  clear_button, end_session_button: sensitive = view.session_buttons_enabled(state)

python3 -m tts_player … --tts-flag / --tts-off-hook   → неизвестные аргументы (argparse, exit 2)
```

**Acceptance Criteria:**
- [ ] список сессий стоит в контейнере главного экрана между рядом служебных кнопок и прокручиваемым списком сообщений
- [ ] две сессии в состоянии → две строки с текстами `session_rows`; у активной — жирный шрифт
- [ ] активация строки неактивной сессии → `select_session` с её id; строки активной → `select_session` с её id (плеер ничего не меняет — проверено его тестами)
- [ ] мусорка → `player.clear()`; [stop] → `player.remove_session(<активная>)`, ровно один вызов
- [ ] без активной сессии мусорка и [stop] неактивны
- [ ] список перестраивается, только когда строки изменились (как `_show_rows` сообщений)
- [ ] `grep -rn "Voicing\|voicing\|tts-flag\|tts_flag\|tts-off-hook\|claude-tts.on" tts_player tests README.md` пуст
- [ ] README: главный экран — список сессий, клик, мусорка, [stop]

**Verify:** `python3 -m unittest tests.test_window_gui tests.test_view -v` → `OK`

**Steps:**

- [ ] **Step 1: Failing acceptance** — GUI-случай: две сессии в состоянии фальшивого плеера → две строки. Падает: списка нет.
- [ ] **Step 2: Список сессий** — `Gtk.ListBox` по образцу списка сообщений (перестройка по изменению строк, жирная активная, клик по одному щелчку).
- [ ] **Step 3: Кнопки** — мусорка — прежняя кнопка с новой подсказкой; [stop] — бывшая `voicing_off_button` (переименовать в `end_session_button`, значок прежний); доступность — по `session_buttons_enabled` в `_show_state`; таймерный опрос флага (`_show_voicing`) удалить.
- [ ] **Step 4: Удалить `Voicing`** — модуль, тест, аргументы `__main__`, фальшивый `FakeVoicing` в GUI-случаях; README.
- [ ] **Step 5: Verify** — команда выше, затем весь набор.
- [ ] **Step 6: Commit** — изменённые и удалённые пути по именам.
