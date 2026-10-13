# TTS Player — озвучка молчит, пока диктую: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** пока идёт диктовка в Claude Code и ещё 3 с после неё, ни ответы, ни свой текст не звучат;
прерванное предложение после этого начинается с начала.

**Architecture:** новый `DictationGate` (один на процесс, как `OutputIdle`) получает от
`DictationWatch` начало и конец диктовки и держит отсчёт тишины после неё. `Player` и
`TextSpeaker` спрашивают `closed()` перед каждым звуком и подписываются на смену: закрылся во
время звука — звук стоп, позиция — начало текущего предложения (начало файла), статус
`playing` остаётся; открылся — звучит запомненное.

**Tech Stack:** Python 3.12, PyGObject (GLib таймер). Новых зависимостей нет.

**Slice:** (2) DictationWatch → DictationGate → Player, TextSpeaker — вторым: `DictationWatch`
уже есть (`f36c05f`), а настройка (3) только меняет число, с которым работает ворота.

**Next slices (not planned here):** (3) settings.json плеера — `dictation_gap_ms` и поле
«Продолжать после диктовки через, мс» на экране настроек.

**Spec:** docs/superpowers/specs/2026-10-13-tts-player-dictation-pause-design.md

**Assumptions:**
- Каждый файл очереди и своего текста — ровно одно предложение: «начало текущего
  предложения» = начало играющего файла (`Player._file_base`, `TextSpeaker._file_base`) — так
  устроены `MessageQueue.locate` и `_Ready`; не риск.
- Ворота звука у `Player` уже есть (`_due` + `_follow_status`, спека pause-others): звук
  ждёт, пока идёт тишина `_gap_id`; диктовка — ещё одно условие тех же ворот; не риск.
- Окно показывает `playing` по `PlayerState.status` и `TextState.status`; статус не меняется
  — окно «как при игре» без правок окна.

## Global Constraints

- Ворота закрыты ⇔ диктовка идёт или после её конца не истекли `gap_ms` (в этом куске
  3000 мс по умолчанию). Новая диктовка до истечения — отсчёт снят, после её конца
  начинается заново.
- Во время закрытых ворот ни `Player`, ни `TextSpeaker` не зовут `Playback.play`.
- Закрылись во время звука: статус остаётся `playing`, на кнопке ⏸, позиция — начало
  текущего предложения. Открылись — звук с этой позиции.
- ⏸ во время закрытых ворот — обычная пауза: после открытия звука нет до ▶; ▶ — с начала
  прерванного предложения. Клик по строке или «следующее» выбирают, что зазвучит; звук — после
  открытия.
- Новый ответ, который плеер начал бы сам, во время закрытых ворот ждёт открытия.
- Другие плееры (MPRIS), стоящие на паузе от озвучки, во время закрытых ворот на паузе и
  остаются (статус `playing` — `Player._speaking`).
- `gate` у `Player` и `TextSpeaker` — необязательный параметр конструктора (`None` — ворот
  нет, поведение как раньше): существующие тесты не меняются.
- Коммиты — `git commit <пути> -m "…"`, по-русски.

**User decisions (already made):**
- Только диктовка в Claude Code; продолжение с начала прерванного предложения; после
  нескольких секунд тишины; свой текст молчит так же; новый ответ ждёт конца диктовки; окно —
  как при игре; время тишины — поле в настройках (кусок 3), по умолчанию 3000 мс.

---

### Task 1: DictationGate — диктовка и отсчёт тишины после неё

**Goal:** объект, который отвечает «звук нельзя» во время диктовки и `gap_ms` после неё и
сообщает подписчикам о смене.

**Files:**
- Create: `tts_player/dictation_gate.py`
- Test: `tests/test_dictation_gate.py`

**Contract:** (2) DictationWatch → DictationGate → Player, TextSpeaker

```
class DictationGate:
    def __init__(self, gap_ms: int = 3000) -> None
    gap_ms: int                                       # новое — с ближайшего отсчёта, идущий не трогается
    def set_dictating(self, active: bool) -> None     # от DictationWatch.on_change
    def closed(self) -> bool                          # диктовка идёт или отсчёт после неё не истёк
    def subscribe(self, on_change: Callable[[], None]) -> None   # смена closed(), в цикле GLib
    def stop(self) -> None                            # выход: таймер снят, колбэков больше нет
```

**Acceptance Criteria:**
- [ ] `set_dictating(True)` → `closed()` True, подписчики позваны один раз
- [ ] `set_dictating(False)` → `closed()` True ещё `gap_ms`, затем False и подписчики позваны один раз
- [ ] `set_dictating(True)` во время отсчёта → отсчёт снят, подписчиков не зовут (ворота и так закрыты); следующий `False` — новый полный отсчёт
- [ ] `gap_ms = 0` → открытие сразу после `set_dictating(False)`
- [ ] повторный `set_dictating` с тем же значением ничего не меняет
- [ ] после `stop()` таймер снят, подписчиков не зовут

**Verify:** `python3 -m unittest tests.test_dictation_gate -v` → все `ok`

**Steps:**
- [ ] **Step 1: Failing acceptance** — тесты по критериям; время — главный цикл GLib с малым `gap_ms` (как в `tests/test_output_idle.py`/`tests/test_sentence_counter.py`).
- [ ] **Step 2: Implement** — по контракту.
- [ ] **Step 3: Verify** — команда выше.
- [ ] **Step 4: Commit** — `tts_player/dictation_gate.py`, `tests/test_dictation_gate.py`.

### Task 2: Player молчит за закрытыми воротами

**Goal:** ответы не звучат во время закрытых ворот и после открытия продолжают с начала
прерванного предложения.

**Files:**
- Modify: `tts_player/player.py` (конструктор, `_follow_status`, подписка, `shutdown`, докстринг класса)
- Test: `tests/test_player.py` (новый класс `PlayerDictationTest`)

**Contract:** сторона чтения (2) у `Player`

```
Player.__init__(..., output: OutputIdle | None = None,
                dictation: DictationGate | None = None) -> None
# ворота звука: _follow_status зовёт _sound() только при
#   self._due and not self._pausing and not self._gap_id and not dictation.closed()
# подписчик ворот: закрылись при звучащем файле → _halt(), _start_file(тот же файл, base, base),
#   статус playing; любая смена → _changed()
```

**Acceptance Criteria:**
- [ ] ворота закрылись во время звука → `Playback.stop`, `state().status == "playing"`, `state().position_s` = начало текущего предложения
- [ ] ворота открылись → `Playback.play` того же файла с позиции 0 в файле
- [ ] ⏸ при закрытых воротах → `paused`; открытие звука не даёт; ▶ после — с начала прерванного предложения
- [ ] новое сообщение, которое при нажатой кнопке автовоспроизведения начинается само, при закрытых воротах не звучит до открытия, затем звучит с начала
- [ ] клик по другой строке при закрытых воротах (статус playing) → выбранное звучит только после открытия
- [ ] следующее предложение растущего сообщения, пришедшее при закрытых воротах, не звучит до открытия
- [ ] `FakePause`: пауза других не снимается, пока ворота закрыты (resume не зовётся)
- [ ] `dictation=None` — весь прежний набор тестов `tests.test_player` проходит без правок

**Verify:** `python3 -m unittest tests.test_player -v` → все `ok`

**Steps:**
- [ ] **Step 1: Failing acceptance** — `PlayerDictationTest(PlayerCase)` с настоящим `DictationGate(gap_ms=0)` или управляемой подделкой с тем же интерфейсом.
- [ ] **Step 2: Implement** — по контракту выше; ворота — одно место (`_follow_status`), без второго пути к `Playback.play`; докстринг класса — абзац о воротах диктовки рядом с абзацем о паузе других.
- [ ] **Step 3: Verify** — команда выше, затем весь набор.
- [ ] **Step 4: Commit** — `tts_player/player.py`, `tests/test_player.py`.

### Task 3: Свой текст молчит за закрытыми воротами

**Goal:** экран «Свой текст» не звучит во время закрытых ворот и после открытия продолжает с
начала прерванного предложения.

**Files:**
- Modify: `tts_player/text_speaker.py`
- Test: `tests/test_text_speaker.py`

**Contract:** сторона чтения (2) у `TextSpeaker`

```
TextSpeaker.__init__(self, runtime_dir: Path, python: Path, worker: Path, player: Player,
                     audio_sink: str | None = None, output: OutputIdle | None = None,
                     dictation: DictationGate | None = None) -> None
# _start(index, position) при закрытых воротах: запоминает index и position, статус playing,
#   Playback.play не зовёт; закрылись при звуке → stop, position = _offset(index), статус playing;
#   открылись и статус playing с запомненным → play с запомненной позиции
```

**Acceptance Criteria:**
- [ ] ворота закрылись во время звука → `Playback.stop`, статус `playing`, позиция = начало предложения
- [ ] ворота открылись → звук того же предложения с его начала
- [ ] первое предложение, синтезированное при закрытых воротах (`synthesizing` → готово), не звучит до открытия
- [ ] ⏸ при закрытых воротах → `paused`, открытие звука не даёт
- [ ] главный плеер во время закрытых ворот остаётся в удержании (`hold`), как при звуке своего текста
- [ ] `dictation=None` — прежние тесты `tests.test_text_speaker` проходят без правок

**Verify:** `python3 -m unittest tests.test_text_speaker -v` → все `ok`

**Steps:**
- [ ] **Step 1: Failing acceptance** — тесты по критериям в стиле существующих тестов `TextSpeaker`.
- [ ] **Step 2: Implement** — по контракту; одно место, где `TextSpeaker` зовёт `Playback.play`.
- [ ] **Step 3: Verify** — команда выше, затем весь набор.
- [ ] **Step 4: Commit** — `tts_player/text_speaker.py`, `tests/test_text_speaker.py`.

### Task 4: Подключение: DictationWatch → DictationGate → Player, TextSpeaker

**Goal:** запущенный плеер замолкает при удержании пробела в Claude Code и продолжает через
3 с после отпускания.

**Files:**
- Modify: `tts_player/__main__.py`
- Modify: `README.md` (раздел «Диктовка Claude Code»: теперь не только журнал, а пауза озвучки)

**Contract:** (2) — проводка, своего артефакта нет; `DictationWatch.on_change` → журнал и
`DictationGate.set_dictating`; один `DictationGate` в `Player` и `TextSpeaker`; `stop()` ворот —
при выходе, после `Player.shutdown` / `TextSpeaker.shutdown` не зовут колбэки.

**Acceptance Criteria:**
- [ ] один `DictationGate` передан и `Player`, и `TextSpeaker`
- [ ] строки журнала «диктовка началась» / «диктовка кончилась» остаются
- [ ] README описывает поведение словами окна: молчит, пока зажат пробел, продолжает с начала предложения через 3 с
- [ ] весь набор тестов проходит; плеер перезапущен и стартует без ошибок в `player.log`

**Verify:** `python3 -m unittest discover -s tests` → `OK`; перезапуск плеера по CLAUDE.md, `tail player.log` без Traceback.

**Steps:**
- [ ] **Step 1: Wire** — `DictationGate` рядом с `OutputIdle`, `on_change` вотча — журнал + `set_dictating`; передать в `Player` и `TextSpeaker`; `stop()` в `finally`.
- [ ] **Step 2: README** — обновить раздел.
- [ ] **Step 3: Verify** — команды выше.
- [ ] **Step 4: Commit** — `tts_player/__main__.py`, `README.md`.
