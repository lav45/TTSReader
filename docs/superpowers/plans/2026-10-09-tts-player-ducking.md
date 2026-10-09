# TTS Player — приглушение чужих звуков, пока плеер говорит. План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** пока плеер играет, громкость чужих потоков снижена до N % от их собственной; N — на экране настроек.

**Architecture:** `tts_player/ducking.py` говорит с PipeWire через `pactl` (JSON-вывод, `LC_ALL=C`),
помнит исходные громкости в памяти и в файле `duck.json` в каталоге времени выполнения.
`Player` зовёт его при входе в `playing` и выходе из него; `__main__.py` при старте возвращает
громкости, оставшиеся после падения. N хранится в файле настроек плеера рядом с хвостом.

**Tech Stack:** Python 3.12 + системный PyGObject (GLib) — как в `master`; `pactl` 16.1
(пакет `pulseaudio-utils`, уже установлен) поверх pipewire-pulse. Новых Python-зависимостей нет.
Роль «говорить со звуковым сервером» мог бы занять `pulsectl` (pip) — отклонён: новая зависимость
вне системных пакетов ради трёх вызовов `pactl`.

**Slice:** контракт (9) «плеер → звуковой сервер» — после (6), потому что берёт N из файла
настроек плеера и поле — с экрана настроек, которые строит (6).

**Next slices (not planned here):** нет.

**Spec:** `docs/superpowers/specs/2026-10-09-tts-player-settings-design.md`

**Assumptions:**
- `LC_ALL=C pactl -f json list sink-inputs` отдаёт массив с `index`, `volume` (`{канал: {"value": int}}`)
  и `properties["application.process.id"]`; у потока живого плеера `application.process.id` = PID
  `python3 -m tts_player` — проверено 2026-10-09 на системе пользователя. Без `LC_ALL=C` pactl
  пишет «Invalid non-ASCII character» — проверено.
- `pactl set-sink-input-volume <index> <v1> [v2 …]` принимает сырые значения по каналам — по
  `pactl --help` 16.1; проверяется тестом на заглушке и вручную пользователем.
- Срез (6) влит: `PlayerSettings`/`Prefs`, экран настроек с полем хвоста, `Player.apply_prefs`.

## Global Constraints

- Контракт (9) из спеки — дословно.
- Тесты не трогают настоящий звуковой сервер: `pactl` — заглушка в `PATH` (скрипт, отдающий
  заданный JSON и пишущий свои вызовы в файл); `duck.json` — во временном каталоге.
- Сбой `pactl` не прерывает и не задерживает воспроизведение: вызовы с таймаутом 2 с.
- Коммит по путям, без `Co-Authored-By`.

**User decisions (already made):**
- Способ — плеер сам приглушает чужие потоки через `pactl`; роли WirePlumber отклонены.
- Процент приглушения — на экране настроек.

## Файлы

| Файл | Кто создаёт / кто вызывает | Назначение |
|---|---|---|
| `tts_player/ducking.py` | создаёт `__main__.py`; вызывает `player.py` | контракт (9) |
| `tts_player/player_settings.py` | `__main__.py`, `window.py` | ключ `duck` в `Prefs` |
| `tts_player/player.py` | `__main__.py`, `window.py` | приглушение на входе и выходе из `playing` |
| `tts_player/window.py` | `__main__.py` | поле процента на экране настроек |

---

### Task 1: Приглушение через `pactl`

**Goal:** `Ducker` снижает и возвращает громкость чужих потоков и подбирает след после падения.

**Files:**
- Create: `tts_player/ducking.py`
- Test: `tests/test_ducking.py`

**Contract:** контракт (9), сторона звукового сервера

```
class Ducker:
    def __init__(self, state_file: Path, own_pid: int) -> None   # state_file = <runtime>/duck.json
    def duck(self, percent: int) -> None      # 0–99; 100 → ничего не делает
    def refresh(self) -> None                 # приглушить потоки, появившиеся после duck
    def restore(self) -> None
    def recover(self) -> None                 # при старте: вернуть громкости из state_file, удалить его
    @property
    def ducked(self) -> bool

pactl:   LC_ALL=C pactl -f json list sink-inputs
         pactl set-sink-input-volume <index> <value> [<value> …]   # по каналам, порядок channel_map
чужой    properties["application.process.id"] != str(own_pid)
duck     запомнить {index: [value по каналам]} → state_file (атомарно) → каждому
         set-sink-input-volume round(value × percent / 100); повторный duck без restore — только refresh
refresh  потоки, которых нет в запомненных, — запомнить и приглушить тем же percent
restore  каждому запомненному, который ещё есть в списке, — запомненные значения; state_file удалить
recover  state_file есть → restore по нему (потоки, которых нет, пропустить) → удалить; битый — удалить, строка в stderr
ошибки   pactl нет / код ≠ 0 / таймаут 2 с / не JSON → строка в stderr, исключение не выходит
```

**Acceptance Criteria:**
- [ ] `duck(30)` при двух чужих и одном своём потоке: два вызова `set-sink-input-volume` со значениями 30 % от исходных по каналам, свой поток не тронут
- [ ] `restore()` возвращает исходные значения; поток, исчезнувший между duck и restore, пропущен; `state_file` удалён
- [ ] `refresh()` приглушает только новый поток
- [ ] `duck(100)` — вызовов `set-sink-input-volume` нет
- [ ] `recover()` по оставленному `state_file` возвращает громкости и удаляет файл; битый файл удалён, в stderr строка
- [ ] заглушка `pactl` с кодом 1, с не-JSON, спящая 3 с — `duck` не бросает, возвращается не позже ~2 с, в stderr строка
- [ ] вызовы `pactl` идут с `LC_ALL=C`

**Verify:** `python3 -m unittest tests.test_ducking -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — заглушка `pactl` в `PATH` из временного каталога.
- [ ] **Step 2: Реализация** — по контракту; запись `state_file` — тем же атомарным приёмом, что в `player_settings.py`.
- [ ] **Step 3: Verify** — команда выше и полный `python3 -m unittest discover -s tests`.
- [ ] **Step 4: Commit** — `tts_player/ducking.py tests/test_ducking.py`.

### Task 2: Плеер приглушает, пока играет

**Goal:** `Player` зовёт `Ducker` на входе в `playing` и выходе из него; N — из `Prefs.duck`.

**Files:**
- Modify: `tts_player/player_settings.py`, `tts_player/player.py`, `tts_player/__main__.py`
- Test: `tests/test_player_settings.py`, `tests/test_player.py`, `tests/test_player_e2e.py`

**Contract:** `Player` ↔ `Ducker` и ключ `duck` файла настроек

```
Prefs.duck: int = 30                    # settings.json: "duck": <целое 0–100>; вне / не int / bool → 30, строка в stderr
Player(…, ducker: Ducker | None)        # None — без приглушения (--no-gui-тесты, нет pactl)
  статус стал playing (из любого другого)     → ducker.duck(prefs.duck)
  статус ушёл из playing (paused/stopped/idle) → ducker.restore()
  пока playing — ducker.refresh() раз в 1 с (GLib таймер; снимается при restore)
  shutdown()                                   → ducker.restore()
  apply_prefs с новым duck во время playing    → restore(), затем duck(новый)
__main__: Ducker(<runtime>/duck.json, os.getpid()); recover() до player.start()
```

**Acceptance Criteria:**
- [ ] `Prefs.duck`: по умолчанию 30; 0 и 100 допустимы; 101 / -1 / `"30"` / `true` → 30 и строка в stderr; `save` пишет `duck`
- [ ] на заглушке `Ducker`: начало воспроизведения → один `duck(30)`; пауза → `restore`; продолжение → `duck`; конец без следующего → `restore`
- [ ] ожидание следующего предложения растущего сообщения (статус `playing`, звук молчит) не вызывает `restore`
- [ ] `shutdown` во время игры вызывает `restore`
- [ ] таймер `refresh` есть только в `playing`
- [ ] e2e процесс плеера получает заглушку `pactl` в `PATH`: после первого сообщения в журнале заглушки есть `set-sink-input-volume`, после конца — возврат исходного значения

**Verify:** `python3 -m unittest tests.test_player_settings tests.test_player tests.test_player_e2e -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — заглушка `Ducker` в юнит-тестах плеера, заглушка `pactl` в e2e.
- [ ] **Step 2: Реализация** — вход и выход из `playing` — в одном месте плеера, где меняется статус, а не в каждой команде.
- [ ] **Step 3: Verify** — команда выше и полный `python3 -m unittest discover -s tests`.
- [ ] **Step 4: Commit** — файлы задачи по путям.

### Task 3: Поле процента на экране настроек

**Goal:** «Приглушать другие звуки до, %» на экране настроек сохраняется в `Prefs.duck`.

**Files:**
- Modify: `tts_player/window.py`, `README.md`
- Test: `tests/gui_window_cases.py`

**Contract:** экран настроек ↔ `PlayerSettings`

```
duck_spin: Gtk.SpinButton 0–100, шаг 5, 0 знаков, подпись «Приглушать другие звуки до, %»;
           значение = prefs.load().duck при открытии экрана; доступен при ошибке настроек claude-tts
«Сохранить»: Prefs(tail, autoplay, duck_spin) → prefs.save → player.apply_prefs
```

**Acceptance Criteria:**
- [ ] поле показывает загруженное значение; «Сохранить» пишет новое и вызывает `apply_prefs` с ним
- [ ] при ошибке настроек claude-tts поле активно и сохраняется
- [ ] README: что делает приглушение, 100 — выключено, восстановление после падения, `pactl` нужен

**Verify:** `python3 -m unittest discover -s tests` → `OK`, GUI-случаи не пропущены; `pgrep broadwayd` пуст

**Steps:**

- [ ] **Step 1: Падающие GUI-случаи** — на `FakePrefs`.
- [ ] **Step 2: Реализация** — по контракту.
- [ ] **Step 3: README**.
- [ ] **Step 4: Verify** — команда выше, вывод — в отчёт.
- [ ] **Step 5: Commit** — `tts_player/window.py README.md tests/gui_window_cases.py`.
