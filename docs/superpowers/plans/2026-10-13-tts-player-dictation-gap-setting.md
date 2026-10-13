# TTS Player — время тишины после диктовки в настройках: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** время тишины после диктовки задаётся полем «Продолжать после диктовки через, мс» на
экране настроек и сохраняется в `settings.json`.

**Architecture:** новый ключ `dictation_gap_ms` в `Prefs`/`PlayerSettings` по образцу `gap_ms`;
`Player.apply_prefs` передаёт его в свои ворота (`DictationGate.gap_ms`) — единственный
владелец применения настроек; поле на экране настроек под «Пауза до и после речи, мс».

**Tech Stack:** Python 3.12, PyGObject (Gtk 3). Новых зависимостей нет.

**Slice:** (3) settings.json плеера — последний кусок карты; (1) и (2) слиты (`f36c05f`, `0686211`).

**Next slices (not planned here):** нет.

**Spec:** docs/superpowers/specs/2026-10-13-tts-player-dictation-pause-design.md

**Assumptions:** none — повторяет устройство `gap_ms` (спека 2026-10-12-tts-player-pause-gap-design.md),
которое уже работает в этом репозитории.

## Global Constraints

- Ключ `dictation_gap_ms`, целое 0–10000, по умолчанию 3000; нет ключа → 3000 без строки в
  журнале; вне пределов или не целое → 3000 и строка в журнале; `save` вне пределов —
  `SettingsError`, файл не трогается (как `gap_ms`).
- Поле «Продолжать после диктовки через, мс» — следующей строкой под «Пауза до и после речи,
  мс», пределы 0–10000, шаг стрелок 100 мс (как `gap_spin`), пишется той же кнопкой
  «Сохранить»; экран остаётся, итог — в блоке внизу, как сейчас.
- Новое значение действует с ближайшего отсчёта, идущий не трогается (свойство `DictationGate.gap_ms`).
- Коммиты — `git commit <пути> -m "…"`, по-русски.

**User decisions (already made):**
- Время тишины — своё поле в настройках по аналогии с паузой, по умолчанию 3000 мс; с `gap_ms` не объединяется.

---

### Task 1: dictation_gap_ms в settings.json и в ворота плеера

**Goal:** значение из `settings.json` доходит до `DictationGate.gap_ms` через `Player.apply_prefs`.

**Files:**
- Modify: `tts_player/player_settings.py`, `tts_player/player.py` (`apply_prefs`)
- Test: `tests/test_player_settings.py`, `tests/test_player.py`

**Contract:** (3) settings.json плеера

```
было   {"tail": …, "autoplay": …, "pause_others": …, "gap_ms": <целое 0–10000>}
стало  {"tail": …, "autoplay": …, "pause_others": …, "gap_ms": <целое 0–10000>,
        "dictation_gap_ms": <целое 0–10000>}
Prefs(..., gap_ms: int = 3000, dictation_gap_ms: int = 3000)
PlayerSettings.DICTATION_GAP_MIN_MS = 0; DICTATION_GAP_MAX_MS = 10000
Player.apply_prefs(prefs) → dictation.gap_ms = prefs.dictation_gap_ms (dictation None — ничего)
```

**Acceptance Criteria:**
- [ ] нет ключа → 3000, без строки в журнале
- [ ] 0 и 10000 читаются как есть; -1, 10001, "3000", 1.5 → 3000 и одна строка WARNING
- [ ] `save` пишет `dictation_gap_ms`; `save` вне пределов — `SettingsError`, файл не меняется
- [ ] `Player.apply_prefs` ставит `gap_ms` воротам; идущий отсчёт не меняется

**Verify:** `python3 -m unittest tests.test_player_settings tests.test_player -v` → все `ok`

**Steps:**
- [ ] **Step 1: Failing acceptance** — тесты по критериям рядом с тестами `gap_ms`.
- [ ] **Step 2: Implement** — по образцу `gap_ms`, докстринг модуля — новый ключ.
- [ ] **Step 3: Verify** — команда выше, затем весь набор.
- [ ] **Step 4: Commit** — четыре файла выше.

### Task 2: Поле «Продолжать после диктовки через, мс»

**Goal:** поле на экране настроек показывает и сохраняет `dictation_gap_ms`.

**Files:**
- Modify: `tts_player/window.py`, `README.md` (разделы «Настройки» и «Диктовка Claude Code»: 3 с → значение поля)
- Test: `tests/gui_window_cases.py` (и `tests/test_window_gui.py`, если кейсы регистрируются там)

**Contract:** (3) — поле окна, артефакт контракта в Task 1; своего артефакта нет.

**Acceptance Criteria:**
- [ ] поле подписано «Продолжать после диктовки через, мс», стоит под «Пауза до и после речи, мс»
- [ ] открытие настроек показывает сохранённое значение
- [ ] «Сохранить» пишет значение в `settings.json` и в плеер (`apply_prefs`), набранное без потери фокуса тоже
- [ ] README называет поле и его значение по умолчанию

**Verify:** `python3 -m unittest discover -s tests` → `OK`

**Steps:**
- [ ] **Step 1: Failing acceptance** — GUI-кейсы по образцу кейсов `gap_spin`.
- [ ] **Step 2: Implement** — поле по образцу `gap_spin`; README.
- [ ] **Step 3: Verify** — весь набор.
- [ ] **Step 4: Commit** — `tts_player/window.py`, тесты, `README.md`.
