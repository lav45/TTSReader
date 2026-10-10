# TTS Player — кнопка автовоспроизведения: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** кнопка ⏩ справа от ⏵/⏸ решает, что плеер делает после конца дорожки: нажата — дальше
по списку и ждёт новый ответ, отжата — стоп, новые ответы отложены, история режется до `tail`.

**Architecture:** положение кнопки — поле `Prefs.autoplay` в `settings.json` плеера (контракт (6)).
`Player.apply_prefs` переносит его в плеер; отжатая кнопка включает для автоматических решений
то же поведение, что сейчас даёт удержание на время своего текста (`_held`: `_starts_by_itself`,
`_stop_after`, отложенная задача хука), и снимает фокус с очереди (`_focused`), чтобы она
резалась до `tail`. Окно показывает `PlayerState.autoplay` и по клику пишет файл и зовёт
`apply_prefs`.

**Tech Stack:** Python 3, PyGObject (GTK 3) — как сейчас; новых зависимостей нет.

**Slice:** контракт (6) «Плеер ↔ его файл настроек — ключ `autoplay` возвращается» — единственный
в спеке; пишущая сторона — кнопка окна и `PlayerSettings.save`, читающая — `PlayerSettings.load`
и `Player`.

**Next slices (not planned here):** нет.

**Spec:** `docs/superpowers/specs/2026-10-10-tts-player-follow-new-design.md`

**Assumptions:**
- Поведение «отжата» уже есть в плеере как удержание своего текста (`hold`): `_starts_by_itself`,
  `_stop_after`, отложенная задача в `_on_hook_task` — проверено чтением `tts_player/player.py`;
  не рискованно, техника работает в репозитории и покрыта тестами `PlayerHoldTest`.
- `Gtk.ToggleButton` и значок `media-seek-forward-symbolic` есть в GTK 3 и в теме Cinnamon —
  не рискованно: остальные значки окна (`emblem-system-symbolic`, `go-previous-symbolic`) из того
  же набора symbolic; проверяется GUI-тестом под broadwayd (значок задан, виджет — ToggleButton).
- `set_active` из кода шлёт сигнал `toggled` — свойство GTK; обработчик обязан не принимать
  его за клик (критерий задачи 3).

## Global Constraints

- Формат `settings.json`: `{"tail": <1–20>, "duck": <0–100>, "autoplay": <true|false>}`; нет
  файла или ключа — `autoplay = true`; `autoplay` не bool — `true` и строка в `player.log`;
  запись атомарная, пишутся все ключи (контракт (6) спеки).
- Положение кнопки одно на плеер; переключение сессии его не меняет.
- Нажатие кнопки звук не начинает; отжатие во время звука дорожку не обрывает.
- Нажатая кнопка не отменяет свой текст (hold), `tts-player stop` и паузу.
- Подсказка кнопки постоянная: «Автовоспроизведение: следующая дорожка и новые ответы».
- Тесты проекта: `python3 -m unittest discover -s tests`; коммит — по путям
  (`git commit <путь>… -m "…"`), без `Co-Authored-By`.

**User decisions (already made):**
- Кнопка-переключатель справа от ⏵/⏸, значок — двойная стрелка вправо (`media-seek-forward-symbolic`).
- Нажата: конец дорожки — следующая; нет следующей — ожидание, новый ответ играет сам; новый при
  звучащем — в очередь; пауза держит.
- Отжата: конец дорожки — стоп («одна дорожка и стоп»; рекомендация «подряд до последней» не
  принята).
- Отжата: новый ответ отложен до ▶ (сначала выбрано «сразу в синтез», пересмотрено пользователем).
- Отжата: история режется до `tail`, как у закрытой папки (рекомендация «копится» не принята).
- Нажатие только включает режим; отжатие — дорожка доигрывает, затем стоп.
- Одна на плеер, по умолчанию нажата, хранится в `settings.json`.
- Сбой записи по клику — режим переключается до выхода, ошибка в `player.log`.
- Подсказка постоянная.
- Отметки ожидания в истории нет.

---

### Task 1: `autoplay` в файле настроек

**Goal:** `PlayerSettings` читает и пишет ключ `autoplay` по контракту (6).

**Files:**
- Modify: `tts_player/player_settings.py`
- Test: `tests/test_player_settings.py`

**Contract:** контракт (6) — формат файла и `Prefs`

```
@dataclass(frozen=True)
class Prefs:
    tail: int = 5
    duck: int = 30
    autoplay: bool = True

settings.json  {"tail": <1–20>, "duck": <0–100>, "autoplay": <true|false>}
  нет файла / нет ключа autoplay   → autoplay = True
  autoplay не bool (в т.ч. 0, 1)   → autoplay = True, WARNING в лог; tail и duck — как есть
  save(prefs)                      → пишет все три ключа атомарно
```

**Acceptance Criteria:**
- [ ] файл без `autoplay` даёт `Prefs.autoplay is True`
- [ ] `{"autoplay": false}` даёт `Prefs.autoplay is False` без предупреждения
- [ ] `{"autoplay": 0}` и `{"autoplay": "no"}` дают `True` и одно WARNING, остальные ключи читаются
- [ ] `save(Prefs(autoplay=False))` пишет `"autoplay": false` рядом с `tail` и `duck`
- [ ] тест `test_autoplay_of_old_versions_is_ignored_without_warning` заменён: контракт поменялся
  (спека, решение о возврате ключа)
- [ ] докстринг модуля описывает формат с `autoplay`, без «прежних версий не читается»

**Verify:** `python3 -m unittest tests.test_player_settings` → `OK`

**Steps:**

- [ ] **Step 1: Failing tests** — по критериям выше; старый тест про игнорирование `autoplay` убрать вместе с заменой.
- [ ] **Step 2: Implement** — поле `Prefs.autoplay`, проверка «bool, а не int» в `load`, запись в `save`; валидация `tail`/`duck` не меняется.
- [ ] **Step 3: Verify** — команда выше.
- [ ] **Step 4: Commit** — `tts_player/player_settings.py tests/test_player_settings.py`.

### Task 2: Плеер следует `autoplay`

**Goal:** `Player` получает `autoplay` из `apply_prefs`, показывает его в состоянии и ведёт себя
по контракту (6).

**Files:**
- Modify: `tts_player/player.py`
- Test: `tests/test_player.py`

**Contract:** поведение плеера по контракту (6)

```
Player.apply_prefs(prefs: Prefs) -> None      # prefs.autoplay — новое положение; звук не трогает
PlayerState.autoplay: bool                    # для окна

autoplay = True   → как сейчас: конец дорожки — следующая; нет — idle, новый ответ играет сам;
                    задача хука активной сессии — в демон; фокус по нынешнему правилу
autoplay = False  → конец дорожки — stopped, текущим становится следующая с позиции 0
                    (как _stop_after); задача хука активной сессии — отложена (deferred);
                    у очереди нет фокуса — trim до tail, текущее не удаляется
True → False      → звучащая доигрывает, затем stopped; отданные демону задачи не отменяются
False → True      → звук не начинается; отложенные остаются отложенными
hold / stop / pause → не меняются ни при каком autoplay
```

**Acceptance Criteria:**
- [ ] отжата: конец дорожки при следующей в списке → статус `stopped`, текущая — следующая, звука нет
- [ ] отжата: задача хука активной сессии → сообщение `deferred`, в очередь демона ничего не записано
- [ ] отжата: в очереди больше `tail` сообщений → лишние старые удалены, текущее на паузе осталось
- [ ] отжатие во время `playing` → звук не остановлен, после конца дорожки — `stopped`
- [ ] нажатие в `idle` или `stopped` → `Playback.play` не вызван; отложенные остались `deferred`
- [ ] нажата и пауза: пришёл новый ответ → статус остаётся `paused`, текущее не сменилось
- [ ] `state().autoplay` совпадает с последним `apply_prefs`; по умолчанию `True`
- [ ] все прежние тесты `tests/test_player.py` проходят без изменений своих проверок

**Verify:** `python3 -m unittest tests.test_player` → `OK`

**Steps:**

- [ ] **Step 1: Failing tests** — по критериям выше, на `FakePlayback` и подставной очереди демона, как соседние тесты `PlayerHoldTest`.
- [ ] **Step 2: Implement** — `autoplay` в плеере из `apply_prefs`; одно место решает «плеер начинает сам» (`_starts_by_itself`) и учитывает и удержание, и `autoplay`; откладывание задачи хука в `_on_hook_task` и фокус в `_focused` — по тому же признаку; докстринги `Player`, `_on_hook_task`, `_focused`, `_stop_after` описывают новое поведение.
- [ ] **Step 3: Verify** — команда выше и весь набор `python3 -m unittest discover -s tests`.
- [ ] **Step 4: Commit** — `tts_player/player.py tests/test_player.py`.

### Task 3: Кнопка ⏩ в окне

**Goal:** переключатель в ряду управления показывает `autoplay` и переключает его.

**Files:**
- Modify: `tts_player/window.py`
- Test: `tests/gui_window_cases.py`

**Contract:** виджет и его клик по контракту (6)

```
PlayerWindow.autoplay_button: Gtk.ToggleButton
  значок:    media-seek-forward-symbolic
  подсказка: "Автовоспроизведение: следующая дорожка и новые ответы"
  место:     ряд управления, справа от play_pause_button (крайний справа)
  active    == PlayerState.autoplay при каждом on_change
  toggled пользователем → prefs = replace(PlayerSettings.load(), autoplay=active)
                          PlayerSettings.save(prefs); SettingsError → WARNING в player.log
                          Player.apply_prefs(prefs)  (и при сбое записи)
  toggled из set_active (показ состояния) → ни save, ни apply_prefs
_save_settings: Prefs(tail, duck, autoplay = текущее положение кнопки)
```

**Acceptance Criteria:**
- [ ] ряд управления: `[время … play_pause_button, autoplay_button]`, кнопка последняя справа
- [ ] у кнопки значок `media-seek-forward-symbolic` и подсказка из контракта
- [ ] клик: `save` вызван с перевёрнутым `autoplay`, затем `apply_prefs` с теми же `Prefs`
- [ ] `save` бросает `SettingsError` → `apply_prefs` всё равно вызван, в лог одно WARNING, кнопка в новом положении
- [ ] `on_change` с другим `autoplay` меняет положение кнопки без вызова `save` и `apply_prefs`
- [ ] «Сохранить» на экране настроек передаёт `autoplay`, равный положению кнопки

**Verify:** `python3 -m unittest tests.test_window_gui` → `OK`

**Steps:**

- [ ] **Step 1: Failing cases** — в `tests/gui_window_cases.py` по критериям выше, рядом с проверками ряда управления (`play_pause_button`).
- [ ] **Step 2: Implement** — кнопка, показ состояния с блокировкой своего сигнала, обработчик клика, `_save_settings` с `autoplay`.
- [ ] **Step 3: Verify** — команда выше и весь набор `python3 -m unittest discover -s tests`.
- [ ] **Step 4: Commit** — `tts_player/window.py tests/gui_window_cases.py`.

### Task 4: README и прежние описания

**Goal:** README описывает кнопку и `autoplay`; ничто не утверждает, что выключателя нет.

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-10-10-tts-player-follow-new-design.md`

**Contract:** контракт (6) — задача документирует его, своего артефакта у неё нет.

**Acceptance Criteria:**
- [ ] «Окно»: ряд управления с ⏩, что значат нажата/отжата, что клик не начинает и не обрывает звук
- [ ] «Настройки плеера»: формат с `autoplay`, значение по умолчанию, сбой записи по клику — в лог
- [ ] «Сессии в плеере» и описание `tail`: отжатая кнопка — отложенные ответы и обрезка до `tail`
- [ ] `grep -n "autoplay\` прежних версий не читается\|выключателя автовоспроизведения нет" README.md` → пусто
- [ ] спека: строка «Статус» — реализовано, с датой

**Verify:** `grep -n "autoplay" README.md` → строки только про новую кнопку

**Steps:**

- [ ] **Step 1: README** — разделы выше; формулировки сверить с поведением из задач 2–3.
- [ ] **Step 2: Спека** — статус «реализовано 2026-10-10, ждёт проверки пользователем».
- [ ] **Step 3: Commit** — `README.md docs/superpowers/specs/2026-10-10-tts-player-follow-new-design.md`.
