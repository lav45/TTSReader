# TTS Player — экран настроек: голос и скорость. План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** в окне плеера — маленькие кнопки «мусорка» и «шестерёнка» у левого края; шестерёнка
открывает в том же окне экран настроек со списком голосов, скоростью и «Сохранить», которые
пишутся в настройки `claude-tts` для всех проектов.

**Architecture:** `tts_player/daemon_settings.py` читает и пишет подмножество
`~/.claude-tts/config.json` и `sessions.d/*.json` по контракту (5). Окно держит два экрана в
`Gtk.Stack` и обращается к настройкам только через `DaemonSettings`; `__main__.py` создаёт его
от каталога `--tts-config-dir`, как уже делает с `--history-dir`.

**Tech Stack:** Python 3.12 + системный PyGObject (GTK3) — как в `master`; JSON — stdlib. Новых
зависимостей нет.

**Slice:** контракт (5) «плеер ↔ настройки `claude-tts`» — первым, потому что экран настроек
нужен обоим контрактам, а (5) — внешний стык с чужим форматом; (6) добавит на готовый экран одно поле.

**Next slices (not planned here):** контракт (6) «плеер ↔ его файл настроек» (размер хвоста).

**Spec:** `docs/superpowers/specs/2026-10-09-tts-player-settings-design.md`

**Assumptions:**
- Формат `config.json` и `sessions.d/*.json` — по `claude-tts` 9.20.1: `config.py`
  (`load_raw_config`, `atomic_write_json` — `.tmp` рядом + `replace`), `cli.py` (`cmd_speed`,
  `cmd_persona`, `set_default_speed`). Проверено 2026-10-09 по коду пакета.
- Демон берёт `speed` и голос из `personas[<имя>]` при синтезе (`daemon.py:446–451`,
  `get_persona_config` читает файл каждый раз). Имя персоны фиксируется при постановке
  сообщения в очередь (`audio.py:676`): сообщения, уже стоящие в очереди, звучат прежним
  голосом. Проверено по коду; вживую — проверка пользователя.
- Гонка с записью самого `claude-tts`: плеер перечитывает файл в момент сохранения; окно между
  чтением и `replace` — миллисекунды. Принято пользователем вместе с выбором «ко всем проектам».
- Иконки `user-trash-symbolic`, `emblem-system-symbolic`, `go-previous-symbolic` — из темы
  Adwaita, есть в системе; не риск: отсутствие иконки GTK показывает заглушку, тесты на имя.

## Global Constraints

- Контракт (5) из спеки — дословно: чтение `personas`/`active_persona`/`speed` активной;
  сохранение — перечитать, `active_persona`, `speed` во все персоны, удалить `project_personas`,
  удалить ключи `persona`/`speed` из `sessions.d/*.json`, каждый файл атомарно (временный файл в
  том же каталоге + `rename`). Остальные ключи и файлы — как были.
- Тесты не трогают настоящий `~/.claude-tts/` и экран пользователя: каталог настроек —
  временный; GUI — через `broadwayd` (`tests/test_window_gui.py`).
- Окно не читает и не пишет файлы `claude-tts` само — только через `DaemonSettings`.
- Коммит по путям, без `Co-Authored-By`.

**User decisions (already made):**
- Кнопка очистки — маленькая, иконка мусорки, у левого края; рядом — маленькая шестерёнка.
- Экран настроек — в том же окне; стрелка назад на главный экран; кнопка «Сохранить».
- Скорость — скорость синтеза `claude-tts`, не темп плеера.
- «Сохранить» — ко всем проектам: прямая запись `config.json` (против моей рекомендации «к
  озвучиваемому проекту»).
- «Сохранить» сбрасывает переопределения проектов и сессий (против «не трогать и показывать»).
- Скорость одна на все голоса (против «у каждого голоса своя»).

## Файлы

| Файл | Кто создаёт / кто вызывает | Назначение |
|---|---|---|
| `tts_player/daemon_settings.py` | создаёт `__main__.py`; вызывает `window.py` | контракт (5) |
| `tts_player/window.py` | `__main__.py` | кнопки, `Gtk.Stack`, экран настроек |
| `tts_player/__main__.py` | `bin/tts-player` | `--tts-config-dir`, передача в окно |
| `tests/test_daemon_settings.py` | `unittest discover` | контракт (5) на временном каталоге |
| `tests/gui_window_cases.py` | `tests/test_window_gui.py` | экран настроек на заглушке `DaemonSettings` |

---

### Task 1: Чтение и запись настроек `claude-tts`

**Goal:** `DaemonSettings` читает голоса, активную персону и скорость и сохраняет выбор по контракту (5).

**Files:**
- Create: `tts_player/daemon_settings.py`
- Test: `tests/test_daemon_settings.py`

**Contract:** контракт (5), сторона плеера

```
<root>/config.json
  {"active_persona": "<имя>",
   "personas": {"<имя>": {"description": "<строка>", "speed": <0.5–4.0>, ...}},
   "project_personas": {...}, ...}
<root>/sessions.d/<сессия>.json
  {"persona": "<имя>", "speed": <число>, ...}

@dataclass(frozen=True)
class Voice:            name: str; description: str        # description "" если нет
@dataclass(frozen=True)
class VoiceSettings:    voices: list[Voice]; active: str; speed: float
class SettingsError(Exception): ...                        # str(e) — причина для экрана, по-русски

class DaemonSettings:
    SPEED_MIN = 0.5
    SPEED_MAX = 4.0
    def __init__(self, root: Path) -> None                 # root = ~/.claude-tts
    def load(self) -> VoiceSettings                        # raises SettingsError
    def save(self, persona: str, speed: float) -> None     # raises SettingsError

load:  voices — в порядке ключей personas; active = active_persona;
       speed = personas[active].speed
       нет файла / не JSON / нет или пустой personas / active не в personas
             → SettingsError
       personas[active].speed вне 0.5–4.0 → SettingsError (персона и значение в тексте)
save:  config.json перечитывается; persona не в personas или speed вне 0.5–4.0
             → SettingsError, ни один файл не изменён
       config.json: active_persona ← persona; personas[*].speed ← speed;
                    project_personas удалён; прочие ключи как были
       sessions.d/*.json: ключи persona, speed удалены; прочие ключи как были;
                    файл без этих ключей не перезаписывается; не JSON — пропущен
       каждый файл: временный в том же каталоге + rename
       ошибка записи → SettingsError; уже записанные файлы не откатываются
```

**Acceptance Criteria:**
- [ ] `load()` на копии настоящего формата (7 персон, `supertonic-3` активна, 1.8) возвращает 7 голосов с описаниями, `active="supertonic-3"`, `speed=1.8`
- [ ] `load()` без файла, на битом JSON, без `personas`, с `active_persona` вне `personas` — `SettingsError` с непустым текстом
- [ ] `save("ru-denis", 1.3)`: `active_persona == "ru-denis"`, у всех персон `speed == 1.3`, `project_personas` нет, остальные ключи `config.json` равны исходным
- [ ] `save` перечитывает файл: ключ, дописанный в `config.json` после `load()`, после `save` на месте
- [ ] `save` удаляет `persona` и `speed` из `sessions.d/*.json`, другие ключи остаются; файл без этих ключей не меняет `mtime`
- [ ] `save` с неизвестной персоной или скоростью 0.4 / 4.1 — `SettingsError`, `config.json` побайтно прежний
- [ ] после `save` в каталогах нет временных файлов; при запрете записи в каталог — `SettingsError`, `config.json` прежний

**Verify:** `python3 -m unittest tests.test_daemon_settings -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — на временном каталоге с копией формата из контракта; настоящий `~/.claude-tts` не читать.
- [ ] **Step 2: Реализация** — по контракту; атомарная запись — временный файл в каталоге цели + `os.replace`, при ошибке временный файл удаляется (владелец — тот, кто его создал).
- [ ] **Step 3: Verify** — команда выше и полный `python3 -m unittest discover -s tests`, вывод — в отчёт.
- [ ] **Step 4: Commit** — `tts_player/daemon_settings.py tests/test_daemon_settings.py`.

### Task 2: Кнопки и экран настроек в окне

**Goal:** мусорка и шестерёнка у левого края; шестерёнка открывает экран настроек в том же окне, «Сохранить» вызывает `DaemonSettings.save`, стрелка возвращает на главный экран.

**Files:**
- Modify: `tts_player/window.py`, `tts_player/__main__.py`, `README.md`
- Test: `tests/gui_window_cases.py`

**Contract:** интерфейс окна к `DaemonSettings` из Task 1 (заглушка в GUI-тестах)

```
PlayerWindow(player, settings: DaemonSettings)
  главный экран: внизу слева clear_button (иконка user-trash-symbolic, без текста,
                 подсказка «Очистить буфер») и settings_button (emblem-system-symbolic,
                 подсказка «Настройки»); clear_button → player.clear()
  settings_button → settings.load(); стек на экран "settings"
  экран "settings": back_button (go-previous-symbolic) · voice_combo (Gtk.ComboBoxText,
                    id = Voice.name, текст «name — description» или «name»)
                    · speed_spin (Gtk.SpinButton 0.5–4.0, шаг 0.1, 2 знака)
                    · save_button «Сохранить» · settings_error (Gtk.Label)
  load() → SettingsError: voice_combo и speed_spin неактивны, save_button неактивна,
                          settings_error = str(ошибки)
  save_button → settings.save(voice_combo.get_active_id(), speed_spin.get_value())
       успех → главный экран; SettingsError → экран остаётся, settings_error = str(ошибки)
  back_button → главный экран, без save
__main__: --tts-config-dir DIR (по умолчанию ~/.claude-tts) → DaemonSettings(DIR)
```

**Acceptance Criteria:**
- [ ] у `clear_button` и `settings_button` нет текста, есть иконки и подсказки из контракта; обе — в одном ряду, первые слева; `clear_button` вызывает `player.clear`
- [ ] клик по шестерёнке: видимый экран — `"settings"`, в `voice_combo` голоса заглушки, выбран `active`, `speed_spin` = `speed`
- [ ] `load()` с `SettingsError`: `voice_combo`, `speed_spin`, `save_button` неактивны, `settings_error` показывает текст
- [ ] «Сохранить» вызывает `save` с выбранным голосом и значением спина и возвращает на главный экран; при `SettingsError` экран остаётся и показывает текст
- [ ] стрелка назад возвращает на главный экран, `save` не вызван
- [ ] старые GUI-случаи проходят с новым конструктором
- [ ] `python3 -m tts_player --help` показывает `--tts-config-dir`; `README.md` — экран настроек и что «Сохранить» меняет (все проекты, сброс переопределений)

**Verify:** `python3 -m unittest discover -s tests` → `OK`, GUI-случаи не пропущены (`broadwayd` есть)

**Steps:**

- [ ] **Step 1: Падающие GUI-случаи** — заглушка с `load`/`save` и записью вызовов, рядом с `FakePlayer`.
- [ ] **Step 2: Реализация** — по контракту; окно не знает путей `claude-tts`.
- [ ] **Step 3: `__main__.py` и README** — опция по образцу `--history-dir`.
- [ ] **Step 4: Verify** — команда выше, вывод — в отчёт; `pgrep broadwayd` пуст после прогона.
- [ ] **Step 5: Commit** — `tts_player/window.py tts_player/__main__.py README.md tests/gui_window_cases.py`.
