# TTS Player — кнопка [stop]: выключить озвучку (`/tts-off`). План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** маленькая кнопка [stop] внизу слева выполняет хук `/tts-off` для озвучиваемой сессии.

**Architecture:** `tts_player/voicing.py` знает флаг `/tmp/claude-tts.on` и путь хука и запускает
`tts-toggle.sh off` отвязанным процессом с `CLAUDE_CODE_SESSION_ID` из флага. Окно спрашивает у
него, включена ли озвучка, и зовёт выключение; `__main__.py` создаёт его от опций с путями, как
`DaemonSettings` от `--tts-config-dir`.

**Tech Stack:** Python 3.12 + системный PyGObject (GTK3, Gio) — как в `master`. Новых зависимостей нет.

**Slice:** контракт (7) «плеер → хук `/tts-off`» — после (8), потому что не зависит от
остальных и закрывает прямое указание пользователя.

**Next slices (not planned here):** кнопка play/pause и выключатель автовоспроизведения;
контракт (6) «плеер ↔ его файл настроек» (хвост и автовоспроизведение).

**Spec:** `docs/superpowers/specs/2026-10-09-tts-player-settings-design.md`

**Assumptions:**
- `tts-toggle.sh off` при флаге == сессии снимает флаг и зовёт `tts-player-hook off` →
  `tts-player stop` → SIGUSR1 этому плееру (`~/.claude/hooks/tts-toggle.sh`, `bin/tts-player-hook`,
  `tts_player/instance.py`) — проверено по коду 2026-10-09. Хук без `CLAUDE_CODE_SESSION_ID`
  выходит с кодом 1.
- Хук работает до 2 с (`tts-player stop` шлёт сигнал и выходит) — процесс отвязан, окно не ждёт.
- Флаг пишет только `tts-toggle.sh on` строкой UUID без перевода строки; читатель обрезает пробелы.

## Global Constraints

- Контракт (7) из спеки — дословно.
- Тесты не трогают живой флаг `/tmp/claude-tts.on`, живой хук и демон: флаг и хук — во временном
  каталоге, хук — заглушка, пишущая окружение и аргументы в файл.
- Окно не знает путей флага и хука — только через `Voicing`.
- Коммит по путям, без `Co-Authored-By`.

**User decisions (already made):**
- Кнопка [stop] — маленькая иконка `system-shutdown-symbolic` внизу слева рядом с мусоркой и
  шестерёнкой, подсказка «Выключить озвучку (/tts-off)»; неактивна, когда флага нет.
- Кнопка выполняет хук `/tts-off`; номер сессии берётся из файла флага.

## Файлы

| Файл | Кто создаёт / кто вызывает | Назначение |
|---|---|---|
| `tts_player/voicing.py` | создаёт `__main__.py`; вызывает `window.py` | контракт (7) |
| `tts_player/window.py` | `__main__.py` | кнопка `voicing_off_button` |
| `tts_player/__main__.py` | `bin/tts-player` | `--tts-flag`, `--tts-off-hook` |
| `tests/test_voicing.py` | `unittest discover` | флаг и запуск хука-заглушки |
| `tests/gui_window_cases.py` | `tests/test_window_gui.py` | кнопка на заглушке `Voicing` |

---

### Task 1: Выключение озвучки через хук

**Goal:** `Voicing` сообщает, включена ли озвучка, и запускает `tts-toggle.sh off` с сессией из флага.

**Files:**
- Create: `tts_player/voicing.py`
- Test: `tests/test_voicing.py`

**Contract:** контракт (7), сторона плеера

```
class Voicing:
    def __init__(self, flag: Path, hook: Path) -> None   # /tmp/claude-tts.on, ~/.claude/hooks/tts-toggle.sh
    def is_on(self) -> bool                              # флаг существует и непуст после strip()
    def turn_off(self) -> None

turn_off:
  флага нет / пуст                → строка в stderr («озвучка уже выключена»), хук не запускается
  иначе → <hook> off, окружение = окружение плеера + CLAUDE_CODE_SESSION_ID=<флаг.strip()>
          процесс отвязан (новая сессия), stdout и stderr → stderr плеера;
          turn_off возвращается сразу, не дожидаясь хука
  хук не запустился (нет файла, нет прав) → строка в stderr с причиной, исключение не выходит
  хук завершился с кодом ≠ 0       → строка в stderr с кодом (в главном цикле GLib)
```

**Acceptance Criteria:**
- [ ] `is_on()` — `False` без флага и с пустым флагом, `True` с UUID
- [ ] `turn_off()` запускает заглушку с аргументом `off` и `CLAUDE_CODE_SESSION_ID` = UUID из флага (без пробелов и перевода строки)
- [ ] `turn_off()` возвращается раньше, чем заглушка, спящая 1 с, завершилась
- [ ] без флага заглушка не запускается
- [ ] нет файла хука / нет права на исполнение — `turn_off()` не бросает, в stderr строка с причиной
- [ ] заглушка с кодом 3 — в stderr строка с кодом 3 после прокрутки главного цикла

**Verify:** `python3 -m unittest tests.test_voicing -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — флаг и заглушка хука во временном каталоге; живой `/tmp/claude-tts.on` не читать.
- [ ] **Step 2: Реализация** — по контракту; запуск — `Gio.Subprocess` или `subprocess.Popen(start_new_session=True)` с ожиданием кода в главном цикле.
- [ ] **Step 3: Verify** — команда выше и полный `python3 -m unittest discover -s tests`.
- [ ] **Step 4: Commit** — `tts_player/voicing.py tests/test_voicing.py`.

### Task 2: Кнопка [stop] в окне

**Goal:** иконка выключения озвучки рядом с мусоркой и шестерёнкой; активна, пока озвучка включена.

**Files:**
- Modify: `tts_player/window.py`, `tts_player/__main__.py`, `README.md`
- Test: `tests/gui_window_cases.py`

**Contract:** интерфейс окна к `Voicing` из Task 1 (заглушка в GUI-тестах)

```
PlayerWindow(player, settings: DaemonSettings, voicing: Voicing)
  voicing_off_button: иконка system-shutdown-symbolic, без текста,
                      подсказка «Выключить озвучку (/tts-off)»;
                      в ряду внизу слева третьей, после clear_button и settings_button
  чувствительность = voicing.is_on(), обновляется при показе окна и по таймеру REFRESH_MS
  клик → voicing.turn_off()
__main__: --tts-flag FILE (по умолчанию /tmp/claude-tts.on)
          --tts-off-hook FILE (по умолчанию ~/.claude/hooks/tts-toggle.sh) → Voicing(...)
```

**Acceptance Criteria:**
- [ ] кнопка без текста, иконка и подсказка из контракта, третья в нижнем ряду слева
- [ ] `is_on()` → `False`: кнопка неактивна; → `True` после тика обновления: активна
- [ ] клик вызывает `turn_off()` один раз
- [ ] существующие GUI-случаи проходят с новым конструктором
- [ ] `python3 -m tts_player --help` показывает обе опции; README — кнопка [stop] и что она делает

**Verify:** `python3 -m unittest discover -s tests` → `OK`, GUI-случаи не пропущены; `pgrep broadwayd` пуст

**Steps:**

- [ ] **Step 1: Падающие GUI-случаи** — `FakeVoicing` с `is_on`/`turn_off` рядом с `FakeSettings`.
- [ ] **Step 2: Реализация** — по контракту.
- [ ] **Step 3: `__main__.py` и README** — опции по образцу `--tts-config-dir`.
- [ ] **Step 4: Verify** — команда выше, вывод — в отчёт.
- [ ] **Step 5: Commit** — `tts_player/window.py tts_player/__main__.py README.md tests/gui_window_cases.py`.
