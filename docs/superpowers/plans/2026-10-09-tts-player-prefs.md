# TTS Player — файл настроек плеера: хвост, автовоспроизведение, кнопка play/pause. План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** размер хвоста и автовоспроизведение настраиваются в окне и живут между запусками;
ряд управления — `[prev] [play/pause] [next]`, выключатель автовоспроизведения справа в ряду служебных кнопок.

**Architecture:** `tts_player/player_settings.py` читает и пишет `settings.json` плеера (контракт
(6)). `Player.apply_prefs` переносит значения в очередь и в правило перехода; `__main__.py` читает
файл до создания `Player`. Окно пишет файл через `PlayerSettings` и сразу отдаёт значения плееру.

**Tech Stack:** Python 3.12 + системный PyGObject (GTK3, GStreamer) — как в `master`; JSON — stdlib.
Новых зависимостей нет.

**Slice:** контракт (6) «плеер ↔ его файл настроек» — последний на карте экрана настроек;
кнопка play/pause идёт в этот же срез, потому что меняет тот же ряд и то же правило «что играть
дальше», что и автовоспроизведение.

**Next slices (not planned here):** нет — карта закрыта после (7) и (8).

**Spec:** `docs/superpowers/specs/2026-10-09-tts-player-settings-design.md`

**Assumptions:**
- Хвост — `Queue.TAIL = 5`, вытеснение `while len(self._started) > self.TAIL` в `Queue.start`
  (`tts_player/queue.py:73–83`) — проверено по коду.
- Автопереход — два места: `Player._message_end` (следующее после конца) и `Player._on_sentence`
  в `idle` (новое в тишине), `tts_player/player.py` — проверено по коду.
- `tts-player play` (`play_if_stopped`, контракт (4)) сейчас зовёт `toggle_play_stop`; после
  слияния кнопок зовёт play/pause — поведение команды не меняется.
- Не риск: атомарная запись JSON уже есть в `tts_player/daemon_settings.py` — тот же приём.

## Global Constraints

- Контракт (6) из спеки — дословно (значения по умолчанию, диапазон 1–20, атомарная запись,
  постепенное уменьшение хвоста, правило autoplay = false).
- Контракт (4) (`tts-player stop` / `play`) не меняется: `stop_playback` и `play_if_stopped` остаются.
- Тесты не читают и не пишут настоящий `~/.config/tts-player/`: юнит-тесты — временный путь;
  e2e-тесты процесса плеера — временный `XDG_CONFIG_HOME` в окружении подпроцесса.
- Окно отдаёт команды `Player` только через его методы; что показывать — решает `tts_player/view.py`.
- Коммит по путям, без `Co-Authored-By`.

**User decisions (already made):**
- Уменьшение хвоста — постепенно; хвост 1–20; файл `~/.config/tts-player/settings.json`.
- Кнопка stop из ряда управления убирается, pause и play — одна кнопка.
- Выключатель автовоспроизведения справа в ряду служебных кнопок (ряд теперь наверху); отвечает только за проигрывание файлов из буфера:
  выключен — плеер сам не начинает ни следующее сообщение, ни новое в тишине.
- Иконка выключателя — `media-playlist-consecutive-symbolic` (выбрана мной по просьбе пользователя).
- Главный экран сверху вниз: служебные кнопки → история воспроизведения → плеер (ползунок,
  кнопки, время); заголовок текущего сообщения убран — дублирует выделенную строку истории.

## Файлы

| Файл | Кто создаёт / кто вызывает | Назначение |
|---|---|---|
| `tts_player/player_settings.py` | создаёт `__main__.py`; вызывает `window.py`, `__main__.py` | контракт (6) |
| `tts_player/queue.py` | `player.py` | хвост из настроек, постепенное вытеснение |
| `tts_player/player.py` | `window.py`, `__main__.py` | `apply_prefs`, `toggle_play_pause`, autoplay |
| `tts_player/view.py` | `window.py` | подпись и доступность play/pause |
| `tts_player/window.py` | `__main__.py` | ряд управления, выключатель, поле хвоста |
| `tts_player/__main__.py` | `bin/tts-player` | `--settings-file`, загрузка до `Player` |

---

### Task 1: Файл настроек плеера

**Goal:** `PlayerSettings` читает и пишет `settings.json` по контракту (6).

**Files:**
- Create: `tts_player/player_settings.py`
- Test: `tests/test_player_settings.py`

**Contract:** контракт (6), сторона файла

```
<path>  = $XDG_CONFIG_HOME/tts-player/settings.json (иначе ~/.config/tts-player/settings.json)
          {"tail": <целое 1–20>, "autoplay": <true|false>}

@dataclass(frozen=True)
class Prefs:  tail: int = 5; autoplay: bool = True

class PlayerSettings:
    TAIL_MIN = 1
    TAIL_MAX = 20
    def __init__(self, path: Path) -> None
    @staticmethod
    def default_path(env: Mapping[str, str]) -> Path
    def load(self) -> Prefs                 # не бросает
    def save(self, prefs: Prefs) -> None    # raises SettingsError (tts_player.daemon_settings)

load:  нет файла → Prefs()
       не JSON / не объект → Prefs(), строка в stderr
       tail вне 1–20 / не int / bool → tail = 5, строка в stderr; autoplay — как есть
       autoplay не bool → autoplay = True, строка в stderr; tail — как есть
save:  tail вне 1–20 или не int → SettingsError, файл не тронут
       каталог создаётся; оба ключа; временный файл в том же каталоге + os.replace;
       временный файл удаляется при любом исключении; OSError → SettingsError
```

**Acceptance Criteria:**
- [ ] `default_path` — с `XDG_CONFIG_HOME` и без него
- [ ] `load()` без файла — `Prefs(5, True)`; на битом JSON, массиве, `tail` 0 / 21 / `"5"` / `true`, `autoplay` `"no"` — значения по контракту и строка в stderr
- [ ] `save(Prefs(3, False))` → `load()` возвращает `Prefs(3, False)`; каталог создан; временных файлов нет
- [ ] `save` с `tail` 0 / 21 — `SettingsError`, файл побайтно прежний
- [ ] нет права записи в каталог — `SettingsError`; не-OSError при записи — временного файла нет

**Verify:** `python3 -m unittest tests.test_player_settings -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — на временном пути; настоящий `~/.config` не читать.
- [ ] **Step 2: Реализация** — по контракту; атомарную запись не копировать из `daemon_settings.py`, а вынести общий помощник, если он там уже отдельная функция, — иначе повторить приём и назвать это в отчёте.
- [ ] **Step 3: Verify** — команда выше и полный `python3 -m unittest discover -s tests`.
- [ ] **Step 4: Commit** — `tts_player/player_settings.py tests/test_player_settings.py` (и `tts_player/daemon_settings.py`, если вынесен помощник).

### Task 2: Хвост, автовоспроизведение и play/pause в плеере

**Goal:** плеер берёт хвост и autoplay из `Prefs` и получает команду play/pause.

**Files:**
- Modify: `tts_player/queue.py`, `tts_player/player.py`, `tts_player/__main__.py`
- Test: `tests/test_queue.py`, `tests/test_player.py`, `tests/test_player_e2e.py`

**Contract:** контракт (6), сторона плеера

```
Queue.tail: int                       # был TAIL = 5; задаётся снаружи, по умолчанию 5
  Queue.start(message): начатое входит в хвост; если начатых больше tail —
                        удаляется одно самое старое (не больше одного за start)
PlayerState.autoplay: bool
Player.apply_prefs(prefs: Prefs) -> None     # queue.tail = prefs.tail; autoplay = prefs.autoplay; on_change
Player.toggle_play_pause() -> None
  playing           → paused, позиция запомнена
  paused            → playing с запомненной позиции
  stopped / idle, есть текущее → playing с курсора; курсор ≥ длины → с 0
  нет текущего      → первое непрослушанное с 0; нет его → ничего
autoplay = False:
  конец сообщения   → текущее прослушано, статус stopped; следующее по порядку → текущее, позиция 0
                      (нет следующего — текущее остаётся, позиция = длина)
  новое в idle      → только в списке, статус и текущее не меняются
play_if_stopped    → как toggle_play_pause из stopped / idle (контракт (4) без изменений)
__main__: --settings-file FILE (по умолчанию PlayerSettings.default_path(os.environ));
          PlayerSettings(...).load() → player.apply_prefs(...) до player.start()
```

**Acceptance Criteria:**
- [ ] `tail` 5 → 2 при пяти начатых: следующий `start` удаляет одно сообщение, а не три; через три новых начатых их 2
- [ ] `tail` 2 → 5: новые начатые не вытесняют, пока их меньше 5
- [ ] `toggle_play_pause` — четыре строки контракта, каждая своим тестом
- [ ] autoplay выключен: после конца сообщения следующее не играет, статус `stopped`, текущее — следующее, позиция 0; на последнем — текущее остаётся
- [ ] autoplay выключен: предложение нового сообщения в `idle` не запускает воспроизведение
- [ ] autoplay включён — прежнее поведение (существующие тесты автоперехода проходят)
- [ ] e2e: процесс плеера с `--settings-file` во временном каталоге, где `autoplay: false`, — после первого сообщения нет строки `playing`; e2e-подпроцессы получают временный `XDG_CONFIG_HOME`

**Verify:** `python3 -m unittest tests.test_queue tests.test_player tests.test_player_e2e -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — очередь, плеер на заглушке воспроизведения, e2e.
- [ ] **Step 2: Реализация** — по контракту; старые `toggle_pause` / `toggle_play_stop` пока остаются (их уберёт Task 3 вместе с кнопками).
- [ ] **Step 3: Verify** — команда выше и полный `python3 -m unittest discover -s tests`.
- [ ] **Step 4: Commit** — файлы задачи по путям.

### Task 3: Ряд управления, выключатель и поле хвоста в окне

**Goal:** новый порядок главного экрана, `[prev] [play/pause] [next]`, выключатель автовоспроизведения в ряду служебных кнопок справа, поле хвоста на экране настроек.

**Files:**
- Modify: `tts_player/window.py`, `tts_player/view.py`, `tts_player/player.py`, `tts_player/__main__.py`, `README.md`
- Test: `tests/gui_window_cases.py`, `tests/test_view.py`, `tests/test_player.py`

**Contract:** окно ↔ `Player` и `PlayerSettings` из Task 1–2

```
PlayerWindow(player, settings: DaemonSettings, prefs: PlayerSettings, …прочие аргументы master)
  главный экран сверху вниз: ряд служебных кнопок (слева clear, settings, voicing_off;
    справа autoplay_button) → message_list (растягивается) → блок плеера:
    scale → ряд управления с position_label справа; title_label и view.current_title удалены
  ряд управления: prev_button · play_pause_button · next_button  (pause_button и play_stop_button нет)
    play_pause_button: подпись view.play_pause_label(state) — «⏸» при playing, иначе «⏵»;
                       активна ⇔ view.play_pause_enabled(state): есть текущее или непрослушанное
                       клик → player.toggle_play_pause()
  ряд служебных кнопок справа: autoplay_button — Gtk.ToggleButton, иконка media-playlist-consecutive-symbolic,
                без текста, подсказка «Автовоспроизведение»; active = state.autoplay
                переключение пользователем → prefs.save(Prefs(tail, новое)) → player.apply_prefs(...)
                SettingsError → строка в stderr, кнопка возвращается к state.autoplay
                обновление из state не вызывает save
  экран настроек: tail_spin (Gtk.SpinButton 1–20, шаг 1, 0 знаков) = prefs.load().tail;
                  доступен и при ошибке настроек claude-tts
  «Сохранить»: prefs.save(Prefs(tail_spin, state.autoplay)) → player.apply_prefs(...),
               затем settings.save(...) — если голос и скорость доступны;
               любая SettingsError → экран остаётся, текст в settings_error
Player: toggle_pause и toggle_play_stop удалены; view: pause_label, play_stop_label, pause_enabled удалены
```

**Acceptance Criteria:**
- [ ] в ряду управления три кнопки в порядке prev, play/pause, next; кнопок pause и play|stop нет
- [ ] порядок детей главного экрана сверху вниз: ряд служебных кнопок, список, ползунок, ряд управления; заголовка текущего нет
- [ ] подпись и доступность play/pause — по `view` для playing, paused, stopped, idle без сообщений, idle с непрослушанным
- [ ] клик по play/pause вызывает `toggle_play_pause`
- [ ] выключатель: иконка, подсказка, справа в ряду служебных кнопок; переключение сохраняет `Prefs` с новым autoplay и вызывает `apply_prefs`; новый снимок состояния меняет `active` без вызова `save`
- [ ] `tail_spin` показывает загруженный хвост; «Сохранить» пишет его и вызывает `apply_prefs`; при ошибке `load()` настроек claude-tts `tail_spin` активен и «Сохранить» пишет только хвост
- [ ] `grep -rn "toggle_play_stop\|toggle_pause\|play_stop_label\|pause_label\|current_title\|title_label" tts_player tests` пуст
- [ ] README: ряд управления, выключатель, поле хвоста, файл `settings.json` и `--settings-file`

**Verify:** `python3 -m unittest discover -s tests` → `OK`, GUI-случаи не пропущены; `pgrep broadwayd` пуст

**Steps:**

- [ ] **Step 1: Падающие тесты** — `view`, GUI-случаи с `FakePlayer` (добавить `toggle_play_pause`, `apply_prefs`) и `FakePrefs`.
- [ ] **Step 2: Реализация** — по контракту; удалить старые методы, подписи и их тесты.
- [ ] **Step 3: `__main__.py` и README** — `PlayerSettings` в окно.
- [ ] **Step 4: Verify** — команда выше, вывод — в отчёт.
- [ ] **Step 5: Commit** — файлы задачи по путям.
