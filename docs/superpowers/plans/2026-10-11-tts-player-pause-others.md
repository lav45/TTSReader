# TTS Player — пауза других плееров вместо приглушения: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** пока плеер говорит, MPRIS-плееры (Chrome, Spotify…), игравшие в момент начала речи,
стоят на паузе; после 5 с тишины плеера они продолжают. Приглушение громкости (`Ducker`, `pactl`,
`duck`) удалено целиком.

**Architecture:** новый класс `MprisPause` (`tts_player/mpris_pause.py`) на сессионной шине
D-Bus через Gio: ставит на паузу играющих, помнит их, забывает тех, кого пользователь запустил
или закрыл сам, и продолжает оставшихся. `Player` вызывает его из `_follow_status` вместо
`Ducker`: начало речи — пауза, и звук главного плеера ждёт ответа; если кто-то встал на паузу —
ещё 3 с тишины; конец речи — таймер 5 с до `resume()`; выход — `resume()` сразу.

**Tech Stack:** Python 3, PyGObject (Gio D-Bus, GLib) — уже в проекте; новых зависимостей нет.
Тесты MPRIS — поддельные плееры на временной шине `Gio.TestDBus` (`dbus-daemon` есть в системе).

**Slice:** контракт (9') «Плеер → MPRIS-плееры: пауза на время речи» — от него зависит флажок
(контракт (6) только включает и выключает эту паузу), и он сам по себе снимает дефект, из-за
которого задача появилась (громкость Chrome залипает на 35 %).

**Next slices (not planned here):**
- контракт (6) — ключ `pause_others` в `settings.json` и флажок «Ставить другие плееры на паузу,
  пока говорю» на экране настроек; до него пауза включена всегда.

**Spec:** `docs/superpowers/specs/2026-10-11-tts-player-pause-others-design.md`

**Assumptions:**
- Поддельный MPRIS-плеер на `Gio.TestDBus` отвечает на `ListNames`, `Pause()`, `Get
  PlaybackStatus` и шлёт `PropertiesChanged`, асинхронные вызовы Gio из Python доходят до
  колбэков в цикле GLib — проверено одноразовым скриптом в scratchpad 2026-10-11 (вывод:
  `[['org.mpris.MediaPlayer2.fake'], ('signal', {'PlaybackStatus': 'Paused'}), 'Paused']`).
- Живой Chrome отдаёт `org.mpris.MediaPlayer2.chromium.instance<pid>` с `PlaybackStatus`,
  `CanPause`, `CanPlay` — проверено `busctl --user get-property` 2026-10-11.
- Таймеры плеера в тестах подменяются моком `GLib` (`self.glib.timeout_add_seconds`, как у
  нынешнего `PlayerDuckingTest`) — техника уже работает в `tests/test_player.py`, не рискованно.
- Сессионная шина есть у процесса плеера: он запускается из сеанса пользователя хуком
  SessionStart — не рискованно; её отсутствие обрабатывается (критерий задачи 3).

## Global Constraints

- Контракт (9') — артефакт задачи 1, без изменений по смыслу: префикс имён
  `org.mpris.MediaPlayer2.`, путь `/org/mpris/MediaPlayer2`, интерфейс
  `org.mpris.MediaPlayer2.Player`, статусы `"Playing" | "Paused" | "Stopped"`.
- Все вызовы D-Bus асинхронные, таймаут 1000 мс; ошибка или таймаут — строка в лог (stderr),
  плеер не запоминается; главный цикл не блокируется.
- 3 с тишины перед первым словом — только если хоть один `Pause()` прошёл; ни одного — звук
  сразу. Отсчёт от момента, когда `MprisPause` сообщил итог.
- 5 с после того, как плеер замолчал, до `resume()`; речь снова началась раньше — таймер снят,
  новой паузы никому, тишины 3 с нет.
- Выход плеера (`shutdown`) — `resume()` сразу, без 5 с.
- В тишине 3 с окно как при игре: статус `playing`, ползунок на позиции начала, звука нет.
  Команды работают как обычно; переход на другое сообщение 3 с не перезапускает.
- Свой текст (удержание, `hold`): пауза ставится, 3 с тишины для своего текста нет.
- Плеер, заигравший во время речи, не трогается; запомненный, ставший `Playing` или `Stopped`
  или ушедший с шины, забывается сразу.
- В файлы состояние паузы не пишется.
- Имена, по образцу соседей: модуль `tts_player/mpris_pause.py`, класс `MprisPause`;
  постоянные `Player.PAUSE_GAP_SECONDS = 3`, `Player.RESUME_DELAY_SECONDS = 5`.
- Тесты проекта: `python3 -m unittest discover -s tests`; коммит — по путям
  (`git commit <путь>… -m "…"`), без `Co-Authored-By`.

**User decisions (already made):**
- Пауза через MPRIS вместо приглушения; приглушение убирается целиком (код, поле в настройках,
  ключ `duck`, раздел README, контракт (9) спеки настроек).
- WirePlumber не обновляется.
- Флажок на экране настроек, по умолчанию включён — следующий срез.
- Действие пользователя главнее паузы плеера; продолжаются только те, кто всё ещё на паузе плеера.
- Пауза ставится один раз в начале речи; заигравшие во время речи не трогаются.
- Снятие паузы — через 5 с тишины, постоянная в коде.
- 3 с тишины в начале, только если кто-то встал на паузу (переключить внимание с видео).
- В эти 3 с окно как при игре, отсчёта не видно.
- Видео в двух вкладках Chrome сразу не рассматривается.

---

### Task 1: `MprisPause` — пауза и продолжение MPRIS-плееров

**Goal:** класс, который ставит на паузу играющих MPRIS-плееров, помнит их, забывает тронутых
пользователем и продолжает оставшихся.

**Files:**
- Create: `tts_player/mpris_pause.py`
- Create: `tests/mpris_fake.py` — поддельный MPRIS-плеер на `Gio.TestDBus` (общий для задач 1 и 2)
- Test: `tests/test_mpris_pause.py`

**Contract:** контракт (9') спеки и интерфейс класса, который зовёт `Player`

```
D-Bus (сессионная шина, все вызовы асинхронные, таймаут 1000 мс):
  список    org.freedesktop.DBus.ListNames → имена с префиксом "org.mpris.MediaPlayer2."
  статус    org.freedesktop.DBus.Properties.Get("org.mpris.MediaPlayer2.Player", "PlaybackStatus")
            на /org/mpris/MediaPlayer2 → "Playing" | "Paused" | "Stopped"
  пауза     org.mpris.MediaPlayer2.Player.Pause()
  продолжить org.mpris.MediaPlayer2.Player.Play()
  сигналы   org.freedesktop.DBus.Properties.PropertiesChanged на /org/mpris/MediaPlayer2
            org.freedesktop.DBus.NameOwnerChanged (new_owner == "" — имя ушло)

class MprisPause:
    def __init__(self, connection: Gio.DBusConnection) -> None
    def pause_playing(self, done: Callable[[bool], None]) -> None
        # каждому "Playing" — Pause(); прошедших запомнить;
        # done(True) — хоть один Pause() прошёл, done(False) — ни одного;
        # done вызывается ровно один раз, после ответов всех (или их таймаутов / ошибок)
    def resume(self) -> None
        # каждому запомненному, чей PlaybackStatus сейчас "Paused", — Play(); список очищен
    @property
    def holding(self) -> bool          # запомненные есть
    def stop(self) -> None             # отписка от сигналов; после stop колбэков нет
```

**Acceptance Criteria:**
- [ ] два поддельных плеера, `Playing` и `Paused`: `pause_playing` ставит на паузу только
      первого, `done(True)`; `resume()` продолжает только его
- [ ] играющих нет (или MPRIS-имён на шине нет) — `done(False)`, ни одного `Pause()`
- [ ] плеер, ответивший на `Pause()` ошибкой D-Bus, не запоминается; при единственном таком —
      `done(False)`; ошибка — строка в лог
- [ ] плеер, не ответивший 1 с, не задерживает `done` дольше таймаута и не запоминается
- [ ] запомненный стал `Playing` (сигнал `PropertiesChanged`) — `resume()` его `Play()` не зовёт,
      даже если потом он снова встал на `Paused`
- [ ] запомненный стал `Stopped` или его имя ушло с шины — `resume()` его не трогает
- [ ] `resume()` повторно и без запомненных — ни одного вызова D-Bus
- [ ] после `stop()` сигналы не меняют список, колбэков нет

**Verify:** `python3 -m unittest tests.test_mpris_pause -v` → все тесты `ok`, `OK`

**Steps:**

- [ ] **Step 1: Поддельный плеер** — `tests/mpris_fake.py`: временная шина `Gio.TestDBus`,
  плеер с именем `org.mpris.MediaPlayer2.<имя>`, `PlaybackStatus`, `Pause`/`Play`, сигнал
  `PropertiesChanged` при смене статуса, режимы «ответить ошибкой» и «не отвечать», журнал
  полученных вызовов; уход имени с шины. Каждый тест поднимает и опускает свою шину.
- [ ] **Step 2: Падающие тесты** — по критериям выше, против артефакта; падают, потому что
  модуля нет, а не из-за опечатки.
- [ ] **Step 3: Реализация** — `tts_player/mpris_pause.py` против артефакта; только асинхронные
  вызовы с таймаутом 1000 мс; ошибки D-Bus — в лог, наружу не выходят.
- [ ] **Step 4: Verify** — команда выше и весь набор `python3 -m unittest discover -s tests`.
- [ ] **Step 5: Commit** — `tts_player/mpris_pause.py tests/mpris_fake.py tests/test_mpris_pause.py`.

### Task 2: `Player` на `MprisPause` вместо `Ducker`

**Goal:** плеер ставит других на паузу в начале речи, ждёт 3 с, если кто-то встал, и продолжает
их через 5 с тишины; `Ducker` из `Player`, `TextSpeaker` и `__main__` убран.

**Files:**
- Modify: `tts_player/player.py` — конструктор, `apply_prefs`, `shutdown`, `_follow_status`,
  `_start_file`, удаление `_follow_orphans`/`_watch*`/`_refresh*`/`_unduck`, докстринг класса
- Modify: `tts_player/__main__.py` — `MprisPause` на `Gio.bus_get_sync(Gio.BusType.SESSION)`
  вместо `Ducker`/`recover()`
- Modify: `tts_player/text_speaker.py:143-145` — докстринг про приглушение
- Test: `tests/test_player.py` — `PlayerDuckingTest` заменяется тестом паузы с подделкой
  `MprisPause`; `tests/test_text_speaker.py` — `FakeDucker` заменяется так же;
  `tests/test_player_e2e.py` или `tests/test_player.py` — сквозной тест с настоящим
  `MprisPause` и `tests/mpris_fake.py`

**Contract:** интерфейс `MprisPause` из задачи 1, как его зовёт `Player`

```
Player(..., pause: MprisPause | None = None, ...)      # None — других не трогать
Player.PAUSE_GAP_SECONDS = 3
Player.RESUME_DELAY_SECONDS = 5

начал звучать (_speaking() стал True):
  таймер продолжения стоит  → снять таймер; pause_playing не звать; звук сразу
  иначе                     → pause.pause_playing(done); звук главного плеера ждёт done
      done(True)            → звук через PAUSE_GAP_SECONDS (в удержании — звука нет и так)
      done(False)           → звук сразу
замолчал (_speaking() стал False) и pause.holding → таймер RESUME_DELAY_SECONDS → pause.resume()
shutdown                  → таймер снят; pause.resume(); pause.stop()
```

**Acceptance Criteria:**
- [ ] начало речи — один вызов `pause_playing`; до `done` `Playback.play` не вызван
- [ ] `done(True)` — `Playback.play` не вызван сразу, вызван по таймеру 3 с с файлом текущего
      сообщения с его позиции; `done(False)` — `Playback.play` сразу
- [ ] в тишине 3 с `state()` — статус `playing`, позиция — начало сообщения
- [ ] пауза пользователя в тишине 3 с — по истечении 3 с звука нет; через 5 с — `resume()`
- [ ] «следующее» или клик по строке в тишине 3 с — `Playback.play` нового сообщения по
      истечении тех же 3 с, а не новых 3 с от клика
- [ ] пауза / stopped / idle — таймер 5 с; по нему `resume()`; речь снова до истечения 5 с —
      таймер снят, `resume()` нет, второго `pause_playing` нет, звук сразу
- [ ] ожидание предложения растущего сообщения (статус `playing` без файла) — ни `resume`, ни
      второго `pause_playing`
- [ ] удержание своего текста из playing — `pause_playing` не повторяется; удержание из idle —
      `pause_playing` один раз, 3 с ничего не задерживают у своего текста
- [ ] `shutdown` — `resume()` сразу, без таймера, и `stop()`
- [ ] сквозной: настоящий `MprisPause` и поддельный плеер `Playing` на `Gio.TestDBus` — начало
      речи ставит его на `Paused`, после конца речи и 5 с он снова `Playing`
- [ ] в `tts_player/` нет `Ducker`, `ducker`, `_unduck`, `duck.json`, `duck-orphans.json`
      кроме `ducking.py` (удаляется в задаче 3)

**Verify:** `python3 -m unittest discover -s tests` → `OK`;
`grep -rn 'ducker\|Ducker\|_unduck' tts_player --include=*.py | grep -v '^tts_player/ducking.py'` → пусто

**Steps:**

- [ ] **Step 1: Падающие тесты** — тест паузы плеера с подделкой `MprisPause` (журнал вызовов,
  ручной вызов `done`) по критериям выше; таймеры — через мок `GLib`, как у нынешнего
  `PlayerDuckingTest`.
- [ ] **Step 2: Реализация в `Player`** — против артефакта; место решения одно —
  `_follow_status` из `_changed`; задержка звука держит текущий файл и позицию, чтобы команды в
  тишине меняли, что заиграет, а не когда.
- [ ] **Step 3: `TextSpeaker` и `__main__`** — `FakeDucker` в `tests/test_text_speaker.py`
  заменить подделкой паузы; `__main__` создаёт `MprisPause` на сессионной шине; шины нет —
  строка в лог и `pause=None`.
- [ ] **Step 4: Сквозной тест** — настоящий `MprisPause` + `tests/mpris_fake.py`.
- [ ] **Step 5: Verify** — обе команды выше.
- [ ] **Step 6: Commit** — изменённые файлы задачи, по путям.

### Task 3: Удалить остатки приглушения

**Goal:** в коде, тестах и окне не остаётся приглушения: модуль, его тесты, `Prefs.duck`, поле
на экране настроек.

**Files:**
- Delete: `tts_player/ducking.py`, `tests/test_ducking.py`
- Modify: `tts_player/player_settings.py` — `Prefs.duck`, `DUCK_MIN/MAX`, `_duck_valid`, докстринг
- Modify: `tts_player/window.py` — `duck_spin`, `DUCK_STEP`, подпись поля, докстринги
- Modify: `tts_player/__main__.py:53` — `help` аргумента настроек
- Test: `tests/test_player_settings.py`, `tests/gui_window_cases.py`

**Contract:** контракт (9') — задача обслуживает его, своего артефакта нет (удаление); формат
`settings.json` — контракт (6), следующий срез; здесь из него только уходит ключ `duck`.

```
нет артефакта: удаление
```

**Acceptance Criteria:**
- [ ] файла `tts_player/ducking.py` и `tests/test_ducking.py` нет
- [ ] `PlayerSettings.load` на `{"tail": 7, "duck": 55, "autoplay": false}` — `Prefs(tail=7,
      autoplay=False)`, без предупреждения о `duck`
- [ ] `PlayerSettings.save` пишет ровно ключи `tail` и `autoplay`
- [ ] на экране настроек нет поля «Приглушать другие звуки до, %»; остальные поля и «Сохранить»
      работают (GUI-тесты экрана настроек проходят)
- [ ] `grep -rniE 'duck|приглуш' tts_player tests` — пусто, кроме теста обратной совместимости в `tests/test_player_settings.py`

**Verify:** `python3 -m unittest discover -s tests` → `OK`;
`grep -rniE 'duck|приглуш' tts_player tests | grep -v '^tests/test_player_settings.py'` → пусто (тест обратной совместимости знает старый ключ)

**Steps:**

- [ ] **Step 1: Тесты настроек** — переписать под формат выше (ключ `duck` игнорируется), GUI-
  тесты — без `duck_spin`; падают, пока `Prefs.duck` есть.
- [ ] **Step 2: Удаление** — модуль, его тесты, поле окна, `Prefs.duck` и проверка.
- [ ] **Step 3: Verify** — обе команды выше.
- [ ] **Step 4: Commit** — удалённые и изменённые файлы задачи, по путям (`git rm` для удалённых).

### Task 4: Документация

**Goal:** README и спеки описывают паузу, а не приглушение.

**Files:**
- Modify: `README.md` — раздел «Приглушение чужих звуков» → «Пауза других плееров»; описание
  `settings.json` и экрана настроек без `duck`
- Modify: `docs/superpowers/specs/2026-10-09-tts-player-settings-design.md` — только строка
  «Статус»: контракт (9) заменён спекой 2026-10-11
- Modify: `docs/superpowers/specs/2026-10-11-tts-player-pause-others-design.md` — строка
  «Статус»: контракт (9') реализован, флажок (6) — следующий срез

**Contract:** контракт (9') — задача обслуживает его, своего артефакта нет.

```
нет артефакта: документация
```

**Acceptance Criteria:**
- [ ] в README раздел «Пауза других плееров»: MPRIS, 3 с тишины только если кто-то встал на
      паузу, продолжение через 5 с, ручной запуск главнее, заигравшие во время речи не трогаются,
      свой текст без 3 с, при падении плеера другие остаются на паузе
- [ ] `grep -niE 'duck|приглуш|pactl' README.md` — пусто
- [ ] в спеке настроек изменена только строка «Статус» (`git diff` по файлу — одна строка)

**Verify:** `grep -niE 'duck|приглуш|pactl' README.md` → пусто;
`git diff --stat docs/superpowers/specs/2026-10-09-tts-player-settings-design.md` → `1 file changed, 1 insertion(+), 1 deletion(-)`

**Steps:**

- [ ] **Step 1: README** — раздел и упоминания по критериям выше, в стиле соседних разделов.
- [ ] **Step 2: Статусы спек** — по строке в каждой.
- [ ] **Step 3: Commit** — три файла, по путям.
