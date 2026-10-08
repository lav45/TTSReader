# TTS Player — окно и трей: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** управлять озвучкой из окна, которое открывается по значку в трее: `prev /
pause / play|stop / next`, ползунок по текущему сообщению, список сообщений, очистка
буфера.

**Architecture:** очередь переходит с модели «выданные элементы» на явный курсор
(текущее сообщение + позиция в нём), иначе `prev` и перемотку назад до склейки не
сделать. `Player` становится контроллером с командами и снимком состояния; окно GTK 3
и значок XApp — тонкий вид над ним. Процесс крутит главный цикл GTK; ключ `--no-gui`
оставляет прежний режим без окна для тестов.

**Tech Stack:** то же, что в `master`, плюс `Gtk` 3.0 и `XApp` 1.0 из системных пакетов
(`gir1.2-xapp-1.0`, уже стоит). Новых пакетов нет.

**Slice:** окно и трей — внутреннее устройство плеера, без контракта на карте. Идёт до
среза (4): `tts-player --tray` запускает именно этот трей.

**Next slices (not planned here):**
- (4) `tts-toggle.sh` → плеер: `tts-player --tray`, `tts-player stop`, единственность экземпляра (flock), демон с `daemon-bin/` в `PATH`.

**Spec:** `docs/superpowers/specs/2026-10-09-tts-player-design.md`

**Assumptions:**
- `XApp.StatusIcon` доступен (`gir1.2-xapp-1.0` 3.2.3), `XApp.StatusIcon.any_monitors()` → `True` — апплет трея Cinnamon работает. Проверено 2026-10-09. Сигнал `activate(button, time)` — по документации libxapp.
- Xvfb в системе нет: автотесты не открывают окна. Вид проверяется через чистую модель отображения; тест на настоящем GTK — только при `TTS_PLAYER_GUI_TESTS=1`, руками.
- Главный цикл GTK 3 (`Gtk.main`) работает на том же `GLib.MainContext`, что `Gio.FileMonitor` и шина GStreamer — источники событий не меняются.

## Global Constraints

- Правила кнопок — из спеки («Кнопки — решение controller'а 2026-10-09»): дословно.
- Хвост — 5 начатых сообщений, включая текущее; с началом 6-го удаляются файлы самого старого.
- `release()` при штатном выходе: непрослушанное возвращается в `inbox/`; прослушанное (доигранное или покинутое по `next`/`prev`) удаляется.
- Существующее поведение остаётся: автопереход, переключение на склейку без сдвига, повтор при отказе вывода, `complete`/`ended`/`switched`/`playing`/`error` в stderr.
- Тесты: без звука (`fakesink`), без окон на экране пользователя, без `~/.claude-tts/` и настоящего `$XDG_RUNTIME_DIR`.
- Коммит по путям, без `Co-Authored-By`.

**User decisions (already made):**
- Окно и значок в трее Cinnamon (XApp), плеер и окно в одном процессе; окно открывается кликом по значку.
- Окно показывает только служебное: время, проект, длительность, позицию; текст не нужен.
- Кнопка очистки всего буфера.
- Python 3 + PyGObject из системных пакетов.

## Файлы

| Файл | Кто создаёт / вызывает | Кто использует |
|---|---|---|
| `tts_player/queue.py` — курсор | `Player` | — |
| `tts_player/player.py` — `Player`, `PlayerState` (перенос из `__main__`) | `__main__` | окно, позже обработчик `tts-player stop` (срез (4)) |
| `tts_player/view.py` — `MessageRow`, `format_*` | окно | тесты |
| `tts_player/window.py` — `PlayerWindow`, `TrayIcon` | `__main__` | — |
| `tts_player/__main__.py` | `python3 -m tts_player` | ручной запуск, e2e, срез (4) |

---

### Task 1: Очередь с курсором

**Goal:** очередь хранит сообщения по порядку и даёт навигацию по ним, длину сообщения и
признак завершённости; модель «выданных элементов» уходит.

**Files:**
- Modify: `tts_player/queue.py`, `tts_player/model.py`
- Test: `tests/test_queue.py`, `tests/test_queue_history.py` (переписать под новый интерфейс, сохранив проверяемое поведение)

**Contract:** интерфейс очереди для `Player`

```
class MessageQueue:
    TAIL = 5
    def add(self, sentence: Sentence) -> None
    def attach(self, entry: HistoryEntry, store: Path) -> Message | None   # как сейчас
    @property
    def messages(self) -> list[Message]          # по recv_ns первого предложения, копия списка
    def following(self, message: Message) -> Message | None
    def preceding(self, message: Message) -> Message | None
    def first_unheard(self) -> Message | None    # самое раннее не прослушанное
    def start(self, message: Message) -> None    # начато: хвост, при 6-м — удалить самое старое
    def mark_heard(self, message: Message) -> None
    def length(self, message: Message) -> float  # duration_s при audio, иначе Σ полученных предложений
    def is_complete(self, message: Message) -> bool
        # audio или created_at заданы, или есть более позднее сообщение (правило new token)
    def locate(self, message: Message, position_s: float) -> tuple[Path, float]
    def offset_of(self, message: Message, sentence: Sentence) -> float
    def release(self, current: Message | None, position_s: float) -> list[Sentence]
        # непрослушанные сообщения — все предложения; current — предложения, кончающиеся
        # после position_s; прослушанное — файлы удаляются; audio-копии удаляются; всё забыто
    def clear(self) -> None                      # удалить все файлы всех сообщений, забыть всё

предложение прослушанного или вытесненного сообщения, пришедшее позже — не добавляется, файл удаляется, INFO
```

**Acceptance Criteria:**
- [ ] `messages` упорядочены по recv_ns первого предложения при любом порядке `add`
- [ ] `following`/`preceding` ходят по `messages`; на краях — `None`
- [ ] `start` 6-го сообщения удаляет файлы самого старого начатого, 2–6 на месте
- [ ] `length` до склейки — сумма длительностей полученных предложений, после — `duration_s`
- [ ] `is_complete`: ложь для последнего сообщения без склейки, истина при появлении более позднего или после `attach`
- [ ] `locate` до склейки попадает в нужное предложение и смещение; позиция за концом прижимается
- [ ] `release(current, pos)`: непрослушанные целиком; у current — только предложения после `pos`; прослушанные удалены; копии склеек удалены
- [ ] `clear()` удаляет все файлы в `store/`, `messages` пуст
- [ ] все проверки `attach` из прежних тестов остаются (сопоставление, допуск 0,05 с, копия, уборка частичной копии)

**Verify:** `python3 -m unittest tests.test_queue tests.test_queue_history -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — новый интерфейс; прежние сценарии `attach` перенести.
- [ ] **Step 2: Реализация** — по контракту; `next_item`, `playing`, `_handed`, `_finished_audio` удаляются. `__main__.py` в этой задаче может временно не работать — его чинит Task 2; коммит допустим только вместе с пометкой в отчёте.
- [ ] **Step 3: Verify** — команда выше, вывод — в отчёт.
- [ ] **Step 4: Commit** — `tts_player/queue.py tts_player/model.py tests/test_queue.py tests/test_queue_history.py`.

### Task 2: Плеер-контроллер

**Goal:** `Player` в `tts_player/player.py` воспроизводит по курсору, выполняет команды
кнопок и отдаёт снимок состояния; всё прежнее поведение сохраняется.

**Files:**
- Create: `tts_player/player.py`
- Modify: `tts_player/__main__.py` (только разбор аргументов и запуск)
- Test: `tests/test_player.py`, существующие e2e

**Contract:** интерфейс контроллера для окна и `__main__`

```
@dataclass(frozen=True)
class PlayerState:
    status: Literal["playing", "paused", "stopped", "idle"]   # idle — нечего играть
    current: Message | None
    position_s: float            # внутри текущего сообщения
    length_s: float              # queue.length(current), 0 без текущего
    messages: list[Message]      # queue.messages
    output_down: bool

class Player:
    def __init__(self, runtime_dir: Path, audio_sink: str | None,
                 history_dir: Path | None, history_db: Path | None) -> None
    on_change: Callable[[PlayerState], None]      # после каждого изменения состояния
    def start(self) -> None
    def stop(self) -> None                         # выход процесса: release в inbox
    def state(self) -> PlayerState
    def toggle_pause(self) -> None
    def toggle_play_stop(self) -> None
    def next(self) -> None
    def prev(self) -> None
    def seek(self, position_s: float) -> None
    def clear(self) -> None

воспроизведение: файл = queue.locate(current, position); конец файла → position = конец
этого файла в сообщении; position < length → следующий файл; иначе is_complete → mark_heard,
following/first_unheard → start; иначе ждать add (статус playing, звук молчит)
```

**Acceptance Criteria:**
- [ ] прежние сценарии e2e (порядок, накопленное, битый WAV, new token, склейка, `switched`, отказ вывода, SIGTERM → inbox) зелёные
- [ ] `toggle_pause` на игре → `paused`, позиция не меняется; повтор → `playing` с той же позиции
- [ ] `toggle_play_stop` на игре → `stopped`, позиция 0, новое сообщение не запускает звук; повтор → играет текущее с 0
- [ ] `next` → следующее сообщение с 0, покинутое — прослушано; на последнем — ничего
- [ ] `prev` → предыдущее с 0; на первом — ничего
- [ ] `seek(p)` до склейки играет нужное предложение со смещением; на паузе остаётся пауза
- [ ] `clear` → `idle`, `store/` пуст, следующее пришедшее предложение играет
- [ ] `on_change` вызывается после каждой команды и смены файла

**Verify:** `python3 -m unittest discover -s tests -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — `FakePlayback` (позиция задаётся тестом), без GLib-ожиданий где можно.
- [ ] **Step 2: Реализация** — перенос `Player` из `__main__` в `player.py`, курсор по контракту Task 1; e2e не меняют наблюдаемый вывод.
- [ ] **Step 3: Verify** — команда выше, вывод — в отчёт.
- [ ] **Step 4: Commit** — `tts_player/player.py tts_player/__main__.py tests/test_player.py` и затронутые тесты.

### Task 3: Модель отображения

**Goal:** чистые функции, превращающие `PlayerState` в то, что показывает окно.

**Files:**
- Create: `tts_player/view.py`
- Test: `tests/test_view.py`

**Contract:** модель отображения

```
@dataclass(frozen=True)
class MessageRow:
    title: str          # "HH:MM:SS  <project>"; без created_at — время первого предложения
    duration: str       # "M:SS"; до склейки — "M:SS…"
    current: bool
    heard: bool

def rows(state: PlayerState) -> list[MessageRow]
def position_label(state: PlayerState) -> str    # "M:SS / M:SS"
def play_stop_label(state: PlayerState) -> str   # "▶" при stopped/idle, "■" иначе
def pause_label(state: PlayerState) -> str       # "⏸" при playing, "⏵" при paused
project — basename каталога проекта; нет → "—"
```

**Acceptance Criteria:**
- [ ] строки списка в порядке `state.messages`, текущая отмечена, прослушанные отмечены
- [ ] длительность до склейки с «…», после — без
- [ ] подписи кнопок и позиции для каждого `status`

**Verify:** `python3 -m unittest tests.test_view -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — `PlayerState` строится в тесте.
- [ ] **Step 2: Реализация** — без импорта Gtk.
- [ ] **Step 3: Verify** — команда выше, вывод — в отчёт.
- [ ] **Step 4: Commit** — `tts_player/view.py tests/test_view.py`.

### Task 4: Окно, трей и запуск

**Goal:** процесс показывает значок в трее; клик открывает окно с кнопками, ползунком,
списком и очисткой буфера; закрытие окна прячет его.

**Files:**
- Create: `tts_player/window.py`
- Modify: `tts_player/__main__.py`
- Test: `tests/test_window_gui.py` (только при `TTS_PLAYER_GUI_TESTS=1`), e2e

**Contract:** запуск процесса

```
python3 -m tts_player [--audio-sink NAME] [--history-dir DIR] [--history-db FILE]
                      [--no-gui] [--show]
  без --no-gui: Gtk.main, значок XApp.StatusIcon ("audio-x-generic", подсказка "TTS Player"),
                окно скрыто; клик левой кнопкой — показать/спрятать; --show — окно сразу видно
  --no-gui:     прежний GLib.MainLoop без Gtk и XApp (e2e)
  нет графической сессии (Gtk.init_check ложь) без --no-gui → exit 1, сообщение в stderr

окно: кнопки prev / pause / play|stop / next, ползунок 0…length_s (перетаскивание → seek),
      подпись позиции, список сообщений (rows), кнопка «Очистить буфер»;
      обновление по on_change и таймером 250 мс для позиции; закрытие — hide
```

**Acceptance Criteria:**
- [ ] e2e идут с `--no-gui` и зелёные
- [ ] `TTS_PLAYER_GUI_TESTS=1`: окно строится, кнопки вызывают соответствующие команды `Player` (через подменённый контроллер), закрытие прячет окно
- [ ] без `TTS_PLAYER_GUI_TESTS` GUI-тест пропускается с причиной, окна не открываются
- [ ] ручная проверка (пользователь): `python3 -m tts_player --show` — значок в трее, окно, кнопки работают на настоящем звуке

**Verify:** `python3 -m unittest discover -s tests -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — e2e с `--no-gui`; GUI-тест под переменной.
- [ ] **Step 2: Реализация** — окно связывается с `Player` только через его контракт и `view`.
- [ ] **Step 3: Verify** — команда выше; GUI-тест и ручная проверка — передаются пользователю.
- [ ] **Step 4: Commit** — `tts_player/window.py tts_player/__main__.py tests/test_window_gui.py` и затронутые e2e.
