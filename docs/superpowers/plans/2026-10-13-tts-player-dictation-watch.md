# TTS Player — слежение за диктовкой Claude Code: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** плеер знает, идёт ли сейчас диктовка в Claude Code, и пишет её начало и конец в журнал.

**Architecture:** новый `DictationWatch` держит процесс `pactl subscribe` через `Gio.Subprocess`
(образец — `SentenceCounter`), на событиях `source-output` разово спрашивает списки потоков и
клиентов в JSON и решает, есть ли поток записи, чей процесс — Claude Code. Смена состояния —
`on_change(bool)` в цикле GLib. В этом куске потребитель — строка журнала в `__main__`.

**Tech Stack:** Python 3.12, PyGObject (Gio/GLib), `pactl` 16.1 (`pulseaudio-utils`, уже в
системе); выбор `pactl` против `pw-dump --monitor`, `pulsectl`, ctypes-libpulse — в спеке.

**Slice:** (1) pactl → DictationWatch — первым, потому что это единственная внешняя
зависимость фичи, и от неё зависят ворота и настройки.

**Next slices (not planned here):** (2) DictationWatch → DictationGate → Player, TextSpeaker;
(3) settings.json плеера — `dictation_gap_ms` и поле на экране настроек.

**Spec:** docs/superpowers/specs/2026-10-13-tts-player-dictation-pause-design.md

**Assumptions:**
- `pactl subscribe` в канал (не терминал) отдаёт строки сразу — проверено 2026-10-13: через
  `| cat` 34 строки пришли до выхода `pactl`.
- `pactl -f json list source-outputs` даёт `client`, `list clients` — `pipewire.sec.pid` —
  проверено на `arecord` (pactl 16.1); без `LC_ALL=C` JSON ломается на русской локали.
- Поток диктовки открывает сам процесс Claude Code: `readlink /proc/702685/exe` =
  `~/.local/share/claude/versions/2.1.296`, поток `PipeWire ALSA [2.1.296]` в журнале
  `pactl subscribe` при удержании пробела.
- `Gio.Subprocess` с построчным чтением stdout в цикле GLib — уже работает в
  `SentenceCounter`, не риск.

## Global Constraints

- Все вызовы `pactl` — с окружением `LC_ALL=C`.
- Главный цикл не ждёт `pactl`: запуск и чтение асинхронные (Gio), как в `SentenceCounter`.
- Диктовка — только поток записи (source-output), у клиента которого
  `readlink(/proc/<pipewire.sec.pid>/exe)` лежит в `~/.local/share/claude/versions/`; имя
  потока не проверяется.
- Сбой (`pactl` нет, `subscribe` вышел, непонятный JSON) — одна строка WARNING в журнале,
  диктовка считается неидущей; если шла — `on_change(False)`.
- Коммиты — `git commit <пути> -m "…"`, по-русски, как в истории.

**User decisions (already made):**
- Озвучку останавливает только диктовка в Claude Code; звонки и другие программы — нет.

---

### Task 1: DictationWatch — чтение событий pactl и решение «диктовка идёт»

**Goal:** класс, который по событиям `pactl subscribe` вызывает `on_change(True/False)` ровно
при смене состояния.

**Files:**
- Create: `tts_player/dictation_watch.py`
- Test: `tests/test_dictation_watch.py`

**Contract:** (1) pactl → DictationWatch

```
$ LC_ALL=C pactl subscribe            # одна строка на событие, построчно
Event 'new' on source-output #161651
Event 'change' on source-output #161651
Event 'remove' on source-output #161651
# на new/remove по source-output (и один раз при start):
$ LC_ALL=C pactl -f json list source-outputs  → [{"index": int, "client": "int", ...}]
$ LC_ALL=C pactl -f json list clients         → [{"index": int,
                                                 "properties": {"pipewire.sec.pid": "str", ...}}]
диктовка идёт ⇔ есть source-output, у клиента которого
                 readlink(/proc/<pipewire.sec.pid>/exe) лежит в <claude_dir>

class DictationWatch:
    def __init__(self, on_change: Callable[[bool], None], *,
                 pactl: str = "pactl",
                 claude_dir: Path = Path.home() / ".local/share/claude/versions") -> None
    def start(self) -> None   # процесс subscribe + первая проверка
    def stop(self) -> None    # процесс убит и подобран; после stop колбэков нет
    @property
    def active(self) -> bool
```

**Acceptance Criteria:**
- [ ] поток записи процесса из `claude_dir` появился → `on_change(True)` один раз; исчез → `on_change(False)` один раз
- [ ] поток записи процесса не из `claude_dir` (как `arecord`) → `on_change` не вызывается
- [ ] события `change` и события не о `source-output` новых проверок не запускают
- [ ] поток Claude Code уже есть при `start()` → `on_change(True)` без события
- [ ] клиент без `pipewire.sec.pid`, исчезнувший процесс (`/proc/<pid>` нет) — не диктовка, без исключения
- [ ] `pactl` не найден или `subscribe` вышел, пока диктовка шла → одна строка WARNING и `on_change(False)`
- [ ] после `stop()` колбэков нет и процесс `subscribe` не жив
- [ ] события, пришедшие во время незавершённой проверки, не теряются: итоговое состояние соответствует последнему списку потоков

**Verify:** `python3 -m unittest tests.test_dictation_watch -v` → все `ok`

**Steps:**

- [ ] **Step 1: Failing acceptance** — поддельный `pactl` (скрипт под текущим интерпретатором, как поддельный воркер в `tests/test_sentence_counter.py`): `subscribe` печатает строки событий по команде теста, `-f json list …` отвечает содержимым файлов теста. Процесс «Claude Code» — копия `/bin/sleep` в временном `claude_dir`, запущенная тестом; её pid — в `pipewire.sec.pid`. Тест падает, потому что модуля нет.
- [ ] **Step 2: Implement** — по контракту выше; асинхронно, как `SentenceCounter`; проверки не перекрываются (пришло событие во время проверки — ещё одна проверка после неё).
- [ ] **Step 3: Verify** — команда выше, затем весь набор `python3 -m unittest discover -s tests`.
- [ ] **Step 4: Commit** — `tts_player/dictation_watch.py`, `tests/test_dictation_watch.py`.

### Task 2: Плеер запускает DictationWatch и пишет диктовку в журнал

**Goal:** запущенный плеер пишет в `player.log` начало и конец каждой диктовки.

**Files:**
- Modify: `tts_player/__main__.py`
- Modify: `README.md` (раздел об устройстве плеера: что такое `DictationWatch`, зачем `pactl`)

**Contract:** (1) pactl → DictationWatch — потребитель в этом куске: строка журнала

```
INFO tts_player.dictation: диктовка началась
INFO tts_player.dictation: диктовка кончилась
```

**Acceptance Criteria:**
- [ ] плеер запускается, `DictationWatch.start()` вызван, при выходе — `stop()`
- [ ] удержание пробела в Claude Code даёт в `player.log` строку «диктовка началась», отпускание — «диктовка кончилась»
- [ ] запись `arecord -d 1 /dev/null` строк о диктовке не даёт
- [ ] без `pactl` в PATH плеер стартует, в журнале одна строка WARNING

**Verify:** `python3 -m unittest discover -s tests` → `OK`; перезапуск плеера по CLAUDE.md, `arecord -d 1 -q /dev/null` → в `$XDG_RUNTIME_DIR/tts-player/player.log` строк о диктовке нет; запись копией исполняемого файла из `~/.local/share/claude/versions/` невозможна без Claude Code — удержание пробела проверяет пользователь.

**Steps:**

- [ ] **Step 1: Wire** — создать `DictationWatch` в `__main__` рядом с `OutputIdle`, `on_change` — строка журнала; `stop()` при выходе, в том же месте, где останавливаются другие владельцы процессов.
- [ ] **Step 2: README** — абзац о `DictationWatch` рядом с описанием `SentenceCounter`/`MprisPause`.
- [ ] **Step 3: Verify** — набор тестов, перезапуск плеера, `arecord` — команды выше.
- [ ] **Step 4: Commit** — `tts_player/__main__.py`, `README.md`.
