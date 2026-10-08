# TTS Player — забор из буфера и воспроизведение: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** процесс плеера забирает предложения из буфера `inbox/` и проигрывает их по
порядку приёма, сообщение за сообщением, с автопереходом к следующему.

**Architecture:** пакет `tts_player/` на Python 3 + PyGObject. `inbox` следит за буфером
через `Gio.FileMonitor`, забирает файлы в собственное хранилище плеера и отдаёт
предложения очереди. `queue` группирует предложения в сообщения и решает, что играть
дальше; `playback` проигрывает файл через GStreamer `playbin`. `__main__` связывает три
части в процесс без окна (окно и трей — следующие срезы).

**Tech Stack:** Python 3.12 + PyGObject из системных пакетов (`python3-gi`,
`gir1.2-gstreamer-1.0`, `gir1.2-gst-plugins-base-1.0`): `Gio.FileMonitor` — слежение за
каталогом (inotify), `Gst` `playbin` — воспроизведение; тесты — `unittest`. Новых
пакетов нет. Альтернативы роли «слежение»: опрос каталога по таймеру — отвергнут
(задержка первого звука), `inotify-tools` — не установлен и даёт второй процесс.

**Slice:** контракт (3) «Обёртка paplay → плеер (буфер)», читающая сторона — пишущая
уже в `master`; без неё подключать обёртку к демону (срез (4)) нельзя: озвучка замолчит.

**Next slices (not planned here):**
- (2) История демона → плеер: признак конца сообщения, замена предложений склейкой, перемотка по всему сообщению.
- (4) `tts-toggle.sh` → плеер: `tts-player --tray`, `tts-player stop`, демон с `daemon-bin/` в `PATH`.
- Окно и трей: кнопки `prev / pause / play|stop / next`, ползунок, очистка буфера — внутреннее устройство плеера, без контракта на карте.

**Spec:** `docs/superpowers/specs/2026-10-09-tts-player-design.md`

**Assumptions:**
- Переименование `.part` → итоговое имя внутри `inbox` `Gio.FileMonitor` с флагом
  `WATCH_MOVES` сообщает событием `renamed`, итоговое имя — во втором файле события;
  `.part` даёт `created`/`changed`. Проверено 2026-10-09 пробой под системным Python.
- `playbin` с `audio-sink=fakesink sync=true` проигрывает WAV до `EOS` без звука —
  проверено той же пробой; на этом держатся тесты без динамика.
- Формат имени предложения задан демоном (`/tmp/tts_queue_{session_id}_{token}_s{i}.wav`,
  `daemon.py:694`); `session_id` — UUID с дефисами, `token` — 6 hex (`secrets.token_hex(3)`).
  Прочитано в коде; не риск.
- Обёртка (`daemon-bin/paplay`, в `master`) кладёт файлы по контракту (3) — проверено её тестами.

## Global Constraints

- Порядок воспроизведения — порядок приёма (`recv_ns` в имени), не номер предложения и не время файла.
- Файл, забранный из `inbox`, принадлежит плееру: лежит в `$XDG_RUNTIME_DIR/tts-player/store/`, удаляет его только плеер.
- Хвост — 5 начатых сообщений, включая текущее; с началом проигрывания 6-го файлы самого старого удаляются. Непроигранные хвостом не ограничены.
- Тесты не играют звук (`fakesink`), не трогают живой демон, `~/.claude-tts/` и настоящий `$XDG_RUNTIME_DIR`.
- Только системный Python 3 и `gi`; никаких pip/venv.
- Коммит — по путям задачи, без `Co-Authored-By`.

**User decisions (already made):**
- Плеер — Python 3 + PyGObject (GTK 3, GStreamer, XApp) из системных пакетов; PySide6 и mpv/ffplay отвергнуты.
- Сообщения в очереди; доигравшее само сменяется следующим автоматически.
- Синтез по предложениям: первый звук — с первого предложения, склейку не ждём.
- Буфер копится, пока плеер не играет; при старте плеер забирает накопленное.

## Файлы

| Файл | Кто создаёт объекты / вызывает | Кто использует |
|---|---|---|
| `tts_player/model.py` — `Sentence`, `Message` | `inbox` создаёт `Sentence`; `queue` создаёт `Message` | `queue`, `playback`, `__main__` |
| `tts_player/inbox.py` — `Inbox` | `__main__` | `queue` получает `Sentence` через колбэк |
| `tts_player/queue.py` — `MessageQueue` | `__main__` | `__main__` спрашивает, что играть |
| `tts_player/playback.py` — `Playback` | `__main__` | — |
| `tts_player/__main__.py` | `python3 -m tts_player` | ручной запуск, e2e-тест; позже `tts-toggle.sh` (срез (4)) |

---

### Task 1: Забор предложений из буфера

**Goal:** `Inbox` при старте и по событиям каталога забирает предложения из `inbox/` в
`store/` и отдаёт их по порядку приёма.

**Files:**
- Create: `tts_player/__init__.py`, `tts_player/model.py`, `tts_player/inbox.py`
- Test: `tests/test_inbox.py`

**Contract:** контракт (3), читающая сторона, и интерфейс `Inbox` для очереди

```
inbox: $XDG_RUNTIME_DIR/tts-player/inbox/   читаются только имена без точки в начале
store: $XDG_RUNTIME_DIR/tts-player/store/   сюда файл переносится (rename) при заборе

имя предложения: ^(?P<recv_ns>\d{19})_tts_queue_(?P<session_id>.+)_(?P<token>[0-9a-f]+)_s(?P<index>\d+)\.wav$
другое имя без точки → WARNING в лог, файл остаётся в inbox

@dataclass(frozen=True)
class Sentence:
    recv_ns: int
    session_id: str
    token: str
    index: int
    path: Path            # уже в store/

class Inbox:
    def __init__(self, runtime_dir: Path, on_sentence: Callable[[Sentence], None]) -> None
    def start(self) -> None   # создаёт каталоги, забирает накопленное по возрастанию recv_ns, затем следит
    def stop(self) -> None
```

**Acceptance Criteria:**
- [ ] файлы, лежавшие в `inbox` до `start()`, приходят в `on_sentence` по возрастанию `recv_ns`, а не по имени предложения
- [ ] файл, положенный обёрткой `daemon-bin/paplay` после `start()`, приходит в `on_sentence` во время работы GLib main loop
- [ ] после забора файла нет в `inbox`, он лежит в `store/` под тем же именем, `Sentence.path` указывает туда
- [ ] `.part`-файлы и другие имена с точкой не забираются и не порождают `on_sentence`
- [ ] имя, не подходящее под шаблон, остаётся в `inbox`, в лог уходит WARNING с именем
- [ ] поля `Sentence` разобраны из имени: `session_id` с дефисами, `token`, `index`, `recv_ns`

**Verify:** `python3 -m unittest tests.test_inbox -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — временный `XDG_RUNTIME_DIR`; файл после `start()` кладётся настоящей обёрткой `daemon-bin/paplay`, main loop крутится ограниченное время.
- [ ] **Step 2: Реализация** — по контракту выше; монитор с `WATCH_MOVES`, итоговое имя при `renamed` — во втором файле события.
- [ ] **Step 3: Verify** — команда выше, вывод — в отчёт.
- [ ] **Step 4: Commit** — `tts_player/__init__.py tts_player/model.py tts_player/inbox.py tests/test_inbox.py`.

### Task 2: Очередь сообщений и хвост

**Goal:** `MessageQueue` группирует предложения в сообщения, выдаёт следующее
предложение для воспроизведения и держит хвост из 5 проигранных сообщений.

**Files:**
- Modify: `tts_player/model.py` (добавить `Message`)
- Create: `tts_player/queue.py`
- Test: `tests/test_queue.py`

**Contract:** интерфейс очереди для `__main__`

```
@dataclass
class Message:
    session_id: str
    token: str
    sentences: list[Sentence]      # по возрастанию recv_ns

class MessageQueue:
    TAIL = 5
    def add(self, sentence: Sentence) -> None
    def next_sentence(self) -> Sentence | None
        # следующее непроигранное предложение текущего сообщения;
        # если в текущем таких нет, а есть более позднее сообщение — переход к нему
        # (текущее считается доигравшим) и его первое предложение;
        # иначе None — ждать новых предложений
    @property
    def current(self) -> Message | None

сообщение = (session_id, token); порядок сообщений — по recv_ns первого предложения
хвост: начатых сообщений (проигранные + текущее) не больше TAIL; с началом 6-го файлы
       самого старого удаляются из store/ и сообщение уходит из очереди
```

**Acceptance Criteria:**
- [ ] предложения одного `(session_id, token)` образуют одно сообщение; другой `token` — новое сообщение
- [ ] `next_sentence()` выдаёт предложения текущего сообщения по `recv_ns`, каждое один раз
- [ ] при исчерпанном текущем сообщении и отсутствии более позднего — `None`; предложение того же сообщения, пришедшее позже, выдаётся следующим вызовом
- [ ] при исчерпанном текущем и наличии более позднего — переход к более позднему
- [ ] с началом 6-го сообщения файлы 1-го удалены с диска, файлы 2–6 на месте; до начала 6-го удалений нет
- [ ] непроигранные сообщения не удаляются, сколько бы их ни было

**Verify:** `python3 -m unittest tests.test_queue -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — `Sentence` с файлами во временном каталоге; без GLib и GStreamer.
- [ ] **Step 2: Реализация** — по контракту выше.
- [ ] **Step 3: Verify** — команда выше, вывод — в отчёт.
- [ ] **Step 4: Commit** — `tts_player/model.py tts_player/queue.py tests/test_queue.py`.

### Task 3: Воспроизведение файла

**Goal:** `Playback` проигрывает один WAV через GStreamer и сообщает о конце или ошибке.

**Files:**
- Create: `tts_player/playback.py`
- Test: `tests/test_playback.py`

**Contract:** интерфейс воспроизведения для `__main__`

```
class Playback:
    def __init__(self, audio_sink: str | None = None) -> None
        # None — системный вывод playbin; имя — элемент GStreamer (тесты: "fakesink", sync=true)
    on_finished: Callable[[Path], None]          # EOS
    on_error: Callable[[Path, str], None]        # ошибка GStreamer, текст ошибки
    def play(self, path: Path) -> None           # прерывает текущее, начинает path
    def stop(self) -> None
```

**Acceptance Criteria:**
- [ ] `play()` валидного WAV при `audio_sink="fakesink"` вызывает `on_finished(path)` один раз, не раньше длительности файла
- [ ] `play()` файла с мусором вместо WAV вызывает `on_error(path, текст)`, `on_finished` не вызывается
- [ ] `play()` во время воспроизведения прерывает текущий файл: `on_finished` приходит только для второго
- [ ] `stop()` останавливает без `on_finished`

**Verify:** `python3 -m unittest tests.test_playback -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — WAV генерируется модулем `wave` (доли секунды тишины); main loop крутится ограниченное время.
- [ ] **Step 2: Реализация** — по контракту выше; сообщения шины — в main loop GLib.
- [ ] **Step 3: Verify** — команда выше, вывод — в отчёт.
- [ ] **Step 4: Commit** — `tts_player/playback.py tests/test_playback.py`.

### Task 4: Процесс плеера — приёмка через стык

**Goal:** `python3 -m tts_player` забирает предложения из буфера и проигрывает их
сообщение за сообщением; приёмка — от обёртки `daemon-bin/paplay` до сыгранного.

**Files:**
- Create: `tts_player/__main__.py`
- Test: `tests/test_player_e2e.py`

**Contract:** вызов процесса и его наблюдаемый вывод

```
python3 -m tts_player [--audio-sink NAME]
  XDG_RUNTIME_DIR — обязателен; нет → exit 1, сообщение в stderr
  работает до SIGTERM/SIGINT → exit 0

stderr, по строке на событие (logging, уровень INFO):
  playing <basename файла в store/>
  error <basename>: <текст ошибки GStreamer>      # после ошибки — следующее предложение

на on_finished и on_error — next_sentence(); None → ждать on_sentence от Inbox
```

**Acceptance Criteria:**
- [ ] предложения A:s0, A:s1, затем B:s0, отданные обёрткой во время работы плеера, звучат в порядке `A s0, A s1, B s0` (строки `playing`)
- [ ] предложение A:s2, пришедшее, пока звучит B, не играется после B: сообщение A уже доиграно (переход по новому `token`)
- [ ] файлы, лежавшие в `inbox` до старта, играются первыми, по `recv_ns`
- [ ] битый WAV даёт строку `error …`, и следующее предложение всё равно звучит
- [ ] без `XDG_RUNTIME_DIR` — exit 1 и сообщение в stderr; SIGTERM — exit 0
- [ ] ручная проверка (не автотест): `python3 -m tts_player` в терминале и `daemon-bin/paplay` на настоящем WAV из `~/.claude-tts/speech_history` — слышен звук

**Verify:** `python3 -m unittest discover -s tests -v` → `OK`, все тесты проекта

**Steps:**

- [ ] **Step 1: Падающая приёмка** — процесс плеера подпроцессом с `--audio-sink fakesink` и временным `XDG_RUNTIME_DIR`; предложения кладёт настоящая обёртка; ожидание строк `playing` с таймаутом.
- [ ] **Step 2: Реализация** — связать `Inbox`, `MessageQueue`, `Playback` по контрактам Task 1–3; новый код в `inbox`/`queue`/`playback` — только по их контрактам.
- [ ] **Step 3: Verify** — команда выше и ручная проверка, вывод — в отчёт.
- [ ] **Step 4: Commit** — `tts_player/__main__.py tests/test_player_e2e.py`.
