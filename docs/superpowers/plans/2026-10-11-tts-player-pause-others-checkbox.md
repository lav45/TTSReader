# TTS Player — флажок «Ставить другие плееры на паузу»: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** флажок на экране настроек включает и выключает паузу других плееров; положение хранится
в `settings.json` ключом `pause_others`, по умолчанию включено.

**Architecture:** `Prefs.pause_others` читает и пишет `PlayerSettings` (контракт (6)).
`Player.apply_prefs` переносит его в плеер: снят — `MprisPause.pause_playing` больше не зовётся,
а поставленные на паузу продолжаются сразу; поставлен — пауза со следующего начала речи. Окно
показывает флажок под «Хвостом очереди» и пишет его кнопкой «Сохранить» вместе с хвостом.

**Tech Stack:** Python 3, PyGObject (GTK 3) — как сейчас; новых зависимостей нет.

**Slice:** контракт (6) «settings.json плеера — ключ `pause_others`» — последний в спеке;
пауза (9') уже в master (`133cb11`).

**Next slices (not planned here):** нет.

**Spec:** `docs/superpowers/specs/2026-10-11-tts-player-pause-others-design.md`

**Assumptions:**
- Чтение и запись ключа устроены как у `autoplay` (`tts_player/player_settings.py`: bool, порча —
  умолчание и строка в лог) — техника в репозитории, не рискованно.
- `Gtk.CheckButton` с подписью в `Gtk.Grid` — штатный виджет GTK 3, GUI-тесты экрана настроек уже
  идут под broadwayd (`tests/test_window_gui.py`) — не рискованно.
- Клик ⏩ пишет файл через `replace(self._prefs.load(), autoplay=…)` (`tts_player/window.py:313`) —
  новый ключ сохраняется сам; проверяется критерием задачи 3.

## Global Constraints

- Формат `settings.json`: `{"tail": <1–20>, "autoplay": <true|false>, "pause_others": <true|false>}`;
  нет файла или ключа — `pause_others = true`; значение не bool — `true` и строка в лог (как
  `autoplay`); лишние ключи (в том числе старый `duck`) игнорируются; запись атомарная, пишутся
  все три ключа.
- Подпись флажка: «Ставить другие плееры на паузу, пока говорю».
- Флажок снят и сохранён, пока другие стоят на паузе, — продолжаются сразу (без 5 с); поставлен
  во время речи — пауза со следующего начала речи.
- Снят — другие плееры не трогаются вовсе: `pause_playing` не вызывается, 3 с тишины нет.
- Тесты проекта: `python3 -m unittest discover -s tests`; каждый тестовый модуль начинается с
  `import tests  # noqa: F401 — …`; коммит — по путям, без `Co-Authored-By`.

**User decisions (already made):**
- Флажок на экране настроек вместо поля приглушения, по умолчанию включён (2026-10-11).
- Пишется той же кнопкой «Сохранить», что и хвост; снят — музыка фоном под озвучку.

**Rulings (controller, not discussed with the user):**
- Флажок снят во время 3 с тишины — тишина обрывается, звук сразу: других больше не держим, ждать
  нечего. Снят, пока ответ `pause_playing` ещё не пришёл, — по ответу поставленные продолжаются
  сразу, 3 с нет.
- Имя ключа `pause_others` — из спеки (моё, записано там).

---

### Task 1: `pause_others` в `Prefs` и `PlayerSettings`

**Goal:** файл настроек хранит положение флажка.

**Files:**
- Modify: `tts_player/player_settings.py`
- Test: `tests/test_player_settings.py`

**Contract:** контракт (6) спеки

```
settings.json  {"tail": <целое 1–20>, "autoplay": <true|false>, "pause_others": <true|false>}
нет файла / нет ключа          → pause_others = true
pause_others не true|false     → pause_others = true, строка в лог; остальные ключи как есть
save                            → пишет tail, autoplay, pause_others

@dataclass(frozen=True)
class Prefs:
    tail: int = 5
    autoplay: bool = True
    pause_others: bool = True
```

**Acceptance Criteria:**
- [ ] нет файла — `Prefs().pause_others is True`
- [ ] `{"tail": 3}` — `pause_others` true без предупреждения
- [ ] `{"pause_others": false}` — false
- [ ] `{"pause_others": 0}`, `"false"`, `null` — true и предупреждение в лог; `tail`/`autoplay` прочитаны
- [ ] `save(Prefs(4, False, False))` пишет ровно `{"tail": 4, "autoplay": false, "pause_others": false}`
- [ ] старый ключ `duck` по-прежнему игнорируется без предупреждения

**Verify:** `python3 -m unittest tests.test_player_settings -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — по критериям выше.
- [ ] **Step 2: Реализация** — по образцу `autoplay`; докстринг модуля — формат с новым ключом.
- [ ] **Step 3: Verify** — команда выше и весь набор.
- [ ] **Step 4: Commit** — оба файла, по путям.

### Task 2: `Player` слушается `pause_others`

**Goal:** снятый флажок отпускает других и больше их не трогает; поставленный включает паузу со
следующего начала речи.

**Files:**
- Modify: `tts_player/player.py` — `apply_prefs`, `_follow_status` и соседи, докстринг класса
- Test: `tests/test_player.py` (класс тестов паузы с подделкой `MprisPause`)

**Contract:** как `apply_prefs` переносит ключ в плеер

```
Player.apply_prefs(prefs)  prefs.pause_others:
  True  → False:  таймер 5 с снят; тишина 3 с снята (звук сразу); pause.resume() сразу;
                  ответ ждущего pause_playing → pause.resume() сразу, без 3 с
  False → True:   ничего сразу; pause_playing — при следующем начале речи
  False, начало речи → pause_playing не вызывается, звук сразу
```

**Acceptance Criteria:**
- [ ] `apply_prefs(Prefs(pause_others=False))` до речи — начало речи не вызывает `pause_playing`, `Playback.play` сразу
- [ ] снят во время речи, когда другие на паузе, — `resume()` сразу, таймер 5 с не ставится
- [ ] снят во время 5 с после речи — таймер снят, `resume()` сразу, ровно один раз
- [ ] снят в тишине 3 с — `Playback.play` сразу, таймер 3 с снят, `resume()` сразу
- [ ] снят, пока ответ `pause_playing` не пришёл, — по `done(True)` `resume()` сразу, таймера 3 с нет, звук сразу
- [ ] поставлен во время речи — `pause_playing` не зовётся до конца этой речи; следующее начало речи — один вызов
- [ ] `apply_prefs` с тем же значением ничего не меняет (нет лишних `resume`)

**Verify:** `python3 -m unittest tests.test_player -v 2>&1 | tail -3` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — по критериям, мок `GLib` и подделка `MprisPause`, как у нынешних тестов паузы.
- [ ] **Step 2: Реализация** — против артефакта; решение по-прежнему в одном месте (`_follow_status` / `apply_prefs` → `_changed`).
- [ ] **Step 3: Verify** — команда выше и весь набор.
- [ ] **Step 4: Commit** — по путям.

### Task 3: Флажок на экране настроек

**Goal:** пользователь видит и меняет положение флажка; «Сохранить» пишет его в файл и в плеер.

**Files:**
- Modify: `tts_player/window.py` — `_settings_screen`, `_open_settings`, `_save_settings`, докстринги
- Test: `tests/gui_window_cases.py`

**Contract:** что пишет «Сохранить» (тот же контракт (6), сторона окна)

```
виджет   Gtk.CheckButton(label="Ставить другие плееры на паузу, пока говорю"), строка 3 сетки, оба столбца
открытие экрана → set_active(PlayerSettings.load().pause_others)
«Сохранить»     → Prefs(tail=…, autoplay=<⏩>, pause_others=<флажок>) → PlayerSettings.save → Player.apply_prefs
клик ⏩          → replace(load(), autoplay=…): pause_others из файла не меняется
```

**Acceptance Criteria:**
- [ ] на экране настроек флажок с подписью «Ставить другие плееры на паузу, пока говорю» под «Хвостом очереди»
- [ ] при каждом открытии экрана флажок показывает значение из файла
- [ ] «Сохранить» со снятым флажком — `save` и `apply_prefs` получают `pause_others=False`
- [ ] флажок доступен и пишется, даже когда голос и скорость не прочитались (как хвост)
- [ ] клик ⏩ не меняет `pause_others` в файле
- [ ] GUI-тесты выполняются, а не пропускаются

**Verify:** `python3 -m unittest tests.test_window_gui -v` → `OK`, без `skipped`

**Steps:**

- [ ] **Step 1: Падающие GUI-тесты** — по критериям.
- [ ] **Step 2: Реализация** — флажок по соседству с `tail_spin`, теми же путями открытия и сохранения.
- [ ] **Step 3: Verify** — команда выше и весь набор.
- [ ] **Step 4: Commit** — по путям.

### Task 4: Документация

**Goal:** README и спека описывают флажок.

**Files:**
- Modify: `README.md` — «Настройки плеера» (формат, флажок на экране), «Пауза других плееров» (убрать «всегда включена», описать флажок), «Настройки голоса» (поля экрана)
- Modify: `docs/superpowers/specs/2026-10-11-tts-player-pause-others-design.md` — строка «Статус»: реализовано полностью

**Contract:** контракт (6) — задача обслуживает его, своего артефакта нет.

```
нет артефакта: документация
```

**Acceptance Criteria:**
- [ ] README: формат `settings.json` с `pause_others`, умолчание true, подпись флажка, поведение при снятии во время паузы и при постановке во время речи
- [ ] в README нет «всегда включена» про паузу
- [ ] в спеке изменена только строка «Статус»

**Verify:** `grep -n 'pause_others' README.md` → строки в «Настройки плеера» и «Пауза других плееров»; `git diff --stat docs/superpowers/specs/2026-10-11-tts-player-pause-others-design.md` → `1 insertion(+), 1 deletion(-)`

**Steps:**

- [ ] **Step 1: README** — по критериям, сверяясь с кодом задач 1–3.
- [ ] **Step 2: Статус спеки** — одна строка.
- [ ] **Step 3: Commit** — оба файла, по путям.
