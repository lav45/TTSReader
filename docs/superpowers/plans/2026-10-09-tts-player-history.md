# TTS Player — история демона: конец сообщения и склейка — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** плеер узнаёт из истории демона, что сообщение синтезировано целиком, получает
его склейку и служебные данные и без сдвига переключает воспроизведение со
предложений на цельный файл.

**Architecture:** новый модуль `history` следит за `~/.claude-tts/speech_history/` и
читает строку из `handy_analysis.db`; очередь сопоставляет запись с сообщением и
копирует склейку в `store/`; `Playback` учится начинать файл с позиции и отдавать
позицию; `Player` переключает файл на лету.

**Tech Stack:** то же, что в `master`: Python 3.12 + системный PyGObject (`Gio`, `Gst`);
`sqlite3` и `wave` из стандартной библиотеки. Новых зависимостей нет.

**Slice:** контракт (2) «История демона → плеер» — читающая сторона; пишущая — демон,
его не меняем. Признак конца сообщения нужен окну (длительность, перемотка по всему
сообщению) и срезу (4).

**Next slices (not planned here):**
- Окно и трей: `prev / pause / play|stop / next`, ползунок, очистка буфера, служебные данные сообщения — внутреннее устройство плеера.
- (4) `tts-toggle.sh` → плеер: `tts-player --tray`, `tts-player stop`, демон с `daemon-bin/` в `PATH`.

**Spec:** `docs/superpowers/specs/2026-10-09-tts-player-design.md`

**Assumptions:**
- `playbin` из `PAUSED` после `ASYNC_DONE` принимает `seek_simple(TIME, FLUSH|ACCURATE, t)`,
  затем `PLAYING` играет с `t`; `query_position` отдаёт позицию. Проверено пробой
  2026-10-09: файл 2 с, старт с 1,5 с → `EOS` через 0,5 с, позиция через 0,2 с — 1,7 с.
- Склейка — те же WAV предложений подряд, тишина перед первым уже в его файле
  (`concat_wavs`, `daemon.py:747–757`): длительность склейки равна сумме длительностей
  предложений прохода. Прочитано в коде демона.
- Демон пишет WAV в `speech_history` раньше строки в базе (`save_speech_wav`, `handy.py`);
  строка появляется в пределах долей секунды. Прочитано в коде; закрывается повтором чтения.
- Продолжение после паузы демона идёт новым `token`; в истории — только последний
  проход (`daemon.py:829–881`). Для плеера это два сообщения; склейка совпадает со
  вторым по сессии и длительности.

## Global Constraints

- Демон и его файлы не меняются; база открывается только на чтение (`mode=ro`).
- Склейка копируется в `store/`; файлы `speech_history` плеер не удаляет и не переносит.
- Замена предложений склейкой — без сдвига позиции; склейка, чья длительность не совпала с суммой предложений сообщения (допуск 0,05 с), не подставляется: сообщение лишь помечается законченным, WARNING.
- Записи истории, существовавшие до старта плеера, не обрабатываются.
- Тесты: каталог истории и база — временные; не трогают `~/.claude-tts/`, живой демон, настоящий `$XDG_RUNTIME_DIR`; звук — только `fakesink`.
- Коммит по путям, без `Co-Authored-By`.

**User decisions (already made):**
- Синтез по предложениям остаётся; конец сообщения — появление склейки и строки в базе.
- Окно показывает только служебное: время, проект, длительность, позицию; текст не нужен.
- Склейку копировать в `store/`, а не подставлять по ссылке (спека, открытые вопросы).
- Хвост — 5 начатых сообщений; файлы сообщения, включая копию склейки, удаляет хвост.

## Файлы

| Файл | Кто создаёт / вызывает | Кто использует |
|---|---|---|
| `tts_player/history.py` — `History`, `HistoryEntry` | `__main__` | `Player` → `queue.attach` |
| `tts_player/model.py` — поля склейки в `Message` | `queue` | `Player`, позже окно |
| `tts_player/queue.py` — `attach`, `locate` | `Player` | — |
| `tts_player/playback.py` — `play(start_s)`, `position()` | `Player` | позже окно (перемотка) |
| `tts_player/__main__.py` — `Player` | `python3 -m tts_player` | — |

---

### Task 1: Чтение истории демона

**Goal:** `History` сообщает о каждой новой склейке в `speech_history` вместе со строкой
базы.

**Files:**
- Create: `tts_player/history.py`
- Test: `tests/test_history.py`

**Contract:** контракт (2), читающая сторона

```
каталог: ~/.claude-tts/speech_history/tts-{created_at:.3f}-{session_id[:40]}.wav
база:    ~/.claude-tts/handy_analysis.db  (sqlite, открывать file:…?mode=ro)
  speech_history(id, file_name, created_at, session_id, project, persona,
                 text, speed, tone, duration_seconds)

@dataclass(frozen=True)
class HistoryEntry:
    path: Path               # файл склейки в speech_history (не наш)
    created_at: float
    session_id: str
    project: str
    duration_s: float

class History:
    def __init__(self, history_dir: Path, db_path: Path,
                 on_entry: Callable[[HistoryEntry], None]) -> None
    def start(self) -> None  # существующие файлы игнорируются; дальше — новые tts-*.wav
    def stop(self) -> None

файл появился, строки ещё нет → повтор чтения каждые 0,2 с до 5 с; не дождались → WARNING, записи нет
база недоступна/занята → WARNING, повтор по тому же расписанию
```

**Acceptance Criteria:**
- [ ] новый `tts-…wav` со строкой в базе → один `on_entry` с полями из строки и `path` к файлу
- [ ] строка, записанная через 1 с после файла, всё равно даёт `on_entry`
- [ ] файл без строки дольше 5 с → WARNING с именем файла, `on_entry` не вызывается
- [ ] файлы, лежавшие до `start()`, не дают `on_entry`
- [ ] имена не вида `tts-*.wav` (и с точкой в начале) игнорируются
- [ ] база не изменяется (открыта только на чтение)

**Verify:** `python3 -m unittest tests.test_history -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — временные каталог и база со схемой выше; main loop GLib с ограничением по времени.
- [ ] **Step 2: Реализация** — по контракту; `Gio.FileMonitor` на каталог, файл копируется демоном (`created`/`changes-done-hint`) — брать по `changes-done-hint` или по `created` с проверкой строки.
- [ ] **Step 3: Verify** — команда выше, вывод — в отчёт.
- [ ] **Step 4: Commit** — `tts_player/history.py tests/test_history.py`.

### Task 2: Сопоставление склейки с сообщением и карта позиций

**Goal:** очередь привязывает запись истории к сообщению, копирует склейку в `store/` и
переводит позицию в сообщении в пару «файл, смещение».

**Files:**
- Modify: `tts_player/model.py`, `tts_player/queue.py`
- Test: `tests/test_queue_history.py`

**Contract:** интерфейс очереди для `Player`

```
@dataclass(eq=False)
class Message:
    session_id: str
    token: str
    sentences: list[Sentence]
    audio: Path | None = None          # копия склейки в store/
    created_at: float | None = None
    project: str | None = None
    duration_s: float | None = None    # из склейки; до неё None

class MessageQueue:
    def attach(self, entry: HistoryEntry, store: Path) -> Message | None
        # сообщение: самое позднее по recv_ns сообщение сессии entry.session_id
        #   без audio, все предложения которого получены раньше entry.created_at;
        # нет такого → None, INFO
        # |entry.duration_s − Σ длительностей предложений| ≤ 0,05 → копия в
        #   store/{entry.path.name}, заполнить audio/created_at/project/duration_s;
        # иначе — только created_at/project, audio=None, WARNING
        # attach законченным считает сообщение в любом случае; сообщение с заполненным
        # created_at повторно не сопоставляется
    def next_item(self) -> Sentence | Message | None
        # (заменяет next_sentence, ревью Task 2) Sentence — играть предложение;
        # Message — началось сообщение со склейкой, играть message.audio с 0;
        # None — ждать. Сообщение со склейкой предложений больше не выдаёт.
    def locate(self, message: Message, position_s: float) -> tuple[Path, float]
        # audio есть → (audio, position_s); иначе предложение и смещение в нём
    def offset_of(self, message: Message, sentence: Sentence) -> float
        # начало предложения в сообщении, с

длительность предложения — по заголовку WAV (wave), читается один раз
хвост и release(): файл audio удаляется вместе с файлами предложений
```

**Acceptance Criteria:**
- [ ] запись сессии S привязывается к самому позднему сообщению S без склейки; сообщения других сессий не затронуты
- [ ] при совпадении длительностей копия лежит в `store/`, исходник в истории на месте, поля сообщения заполнены
- [ ] при расхождении длительностей больше 0,05 с `audio` остаётся `None`, WARNING, `created_at`/`project` заполнены
- [ ] запись без подходящего сообщения → `None`, ничего не скопировано
- [ ] `locate` до склейки: позиция внутри 2-го предложения даёт его файл и смещение от его начала; после склейки — `(audio, position_s)`
- [ ] вытеснение из хвоста и `release()` удаляют копию склейки

**Verify:** `python3 -m unittest tests.test_queue_history tests.test_queue -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — WAV разной длины модулем `wave`; `HistoryEntry` строится в тесте.
- [ ] **Step 2: Реализация** — по контракту; существующие тесты очереди остаются зелёными.
- [ ] **Step 3: Verify** — команда выше, вывод — в отчёт.
- [ ] **Step 4: Commit** — `tts_player/model.py tts_player/queue.py tests/test_queue_history.py` (и `tests/test_queue.py`, если менялся).

### Task 3: Старт с позиции и позиция воспроизведения

**Goal:** `Playback` начинает файл с заданной секунды и отдаёт текущую позицию.

**Files:**
- Modify: `tts_player/playback.py`
- Test: `tests/test_playback.py`

**Contract:** расширение интерфейса воспроизведения

```
class Playback:
    def play(self, path: Path, start_s: float = 0.0) -> None
        # start_s > 0: PAUSED → ASYNC_DONE → seek_simple(TIME, FLUSH|ACCURATE) → PLAYING
    def position(self) -> float | None       # секунды от начала файла; None — ничего не играет
```

**Acceptance Criteria:**
- [ ] файл 1 с, `start_s=0.6` → `on_finished` через 0,4 с (±0,15), не через 1 с
- [ ] `position()` во время игры растёт и лежит в пределах файла; после `stop()` — `None`
- [ ] `start_s` за концом файла → `on_finished` без ошибки
- [ ] существующие тесты `Playback` зелёные

**Verify:** `python3 -m unittest tests.test_playback -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — `fakesink sync=true`, WAV модулем `wave`.
- [ ] **Step 2: Реализация** — по контракту; ожидание `ASYNC_DONE` — через шину в main loop, не блокирующим `timed_pop`.
- [ ] **Step 3: Verify** — команда выше, вывод — в отчёт.
- [ ] **Step 4: Commit** — `tts_player/playback.py tests/test_playback.py`.

### Task 4: Переключение на склейку — приёмка через стык

**Goal:** процесс плеера, получив склейку текущего сообщения, без сдвига переходит на
цельный файл; приёмка — от записи в истории до строки в журнале плеера.

**Files:**
- Modify: `tts_player/__main__.py`
- Test: `tests/test_player_history_e2e.py`

**Contract:** вызов процесса и наблюдаемый вывод

```
python3 -m tts_player [--audio-sink NAME] [--history-dir DIR] [--history-db FILE]
  --history-dir по умолчанию ~/.claude-tts/speech_history
  --history-db  по умолчанию ~/.claude-tts/handy_analysis.db

stderr (INFO), новые строки:
  complete <session_id> <token> <duration_s:.2f>s          # attach с подстановкой склейки
  ended <session_id> <token>                                # attach без подстановки
  switched <basename склейки> at <позиция:.2f>              # переход играющего сообщения на склейку
склейка пришла для не играющего сообщения → его следующий запуск идёт с audio от 0
```

**Acceptance Criteria:**
- [ ] сообщение из трёх предложений по 0,5 с; во время 2-го в историю приходит склейка 1,5 с → строки `complete …` и `switched … at P`, где P ∈ [0,5; 1,0]; следующей строкой `playing` идёт уже следующее сообщение, а не 3-е предложение
- [ ] склейка с другой длительностью → `ended …`, предложения доигрывают как раньше
- [ ] склейка для сообщения в очереди (ещё не начатого) → при его старте `playing <basename склейки>`
- [ ] запись истории до старта плеера не даёт строк
- [ ] все существующие тесты зелёные

**Verify:** `python3 -m unittest discover -s tests -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающая приёмка** — процесс с `--audio-sink fakesink`, временными `XDG_RUNTIME_DIR`, `--history-dir`, `--history-db`; предложения кладёт `daemon-bin/paplay`; склейку и строку пишет тест, как демон (файл, затем строка).
- [ ] **Step 2: Реализация** — `Player` связывает `History` → `queue.attach` → при играющем сообщении: позиция = `offset_of(текущее предложение)` + `playback.position()`, затем `play(audio, start_s=позиция)`.
- [ ] **Step 3: Verify** — команда выше, вывод — в отчёт.
- [ ] **Step 4: Commit** — `tts_player/__main__.py tests/test_player_history_e2e.py`.
