# TTS Player — экран «Свой текст». План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** экран с полем ввода и плеером `[play|pause]` + ползунок озвучивает введённый текст голосом активной персоны; дорожка живёт только на этом экране.

**Architecture:** `tts_player/synth_worker.py` запускается интерпретатором `claude-tts` и пишет WAV по
предложениям, сообщая о них строками JSON (контракт (10)). `tts_player/text_speaker.py` в процессе
плеера запускает его, играет готовые файлы своим `Playback` и держит главный `Player` на паузе.
Окно добавляет кнопку в ряд служебных кнопок и экран в `Gtk.Stack`.

**Tech Stack:** Python 3.12 + системный PyGObject (GTK3, GLib, GStreamer) — как в `master`; процесс
синтеза — Python-окружение `claude-tts` 9.20.1 (uv tool), его `bridge.split_sentences` и
`daemon.daemon_generate_speech`. Новых зависимостей нет.

**Slice:** контракт (10) «плеер ↔ процесс синтеза своего текста» — после (6) и (9): экран встаёт в
ряд служебных кнопок, который (6) переносит наверх.

**Next slices (not planned here):** нет.

**Spec:** `docs/superpowers/specs/2026-10-09-tts-player-settings-design.md`

**Assumptions:**
- `daemon_generate_speech(text, persona, output_file) -> bool` из окружения `claude-tts` пишет WAV:
  два предложения, `supertonic-3`, 1,85 с с загрузкой модели — проверено 2026-10-09 пробным вызовом.
- `claude_code_tts.bridge.split_sentences(text) -> list[str]` — тем же разбиением режет сообщения
  демон (`daemon.py:805`) — проверено по коду.
- `active_persona` берётся из `claude_code_tts.config.load_raw_config()` — проверено по коду.
- Срезы (6) и (9) влиты: ряд служебных кнопок наверху, `Player.toggle_play_pause`, приглушение.

## Global Constraints

- Контракт (10) из спеки — дословно.
- Тесты не запускают настоящий синтез и не трогают `~/.claude-tts`: процесс синтеза в тестах
  запускается системным `python3` с поддельным пакетом `claude_code_tts` в `PYTHONPATH` (временный
  каталог; `split_sentences`, `daemon_generate_speech` пишет короткий WAV, `load_raw_config`).
- Свой текст в списке главного экрана не появляется: файлы — только в `<runtime>/text/`, не в `inbox/`.
- Окно отдаёт команды `Player` и `TextSpeaker` только через их методы.
- Коммит по путям, без `Co-Authored-By`.

**User decisions (already made):**
- Кнопка в ряду служебных кнопок открывает экран: поле ввода, снизу ползунок и `[play|pause]`.
- Дорожка привязана только к этому экрану; изменённый текст озвучивается заново.
- Синтез — внутренней функцией пакета `claude-tts` отдельным процессом.
- Play своего текста ставит главный плеер на паузу; после — главный остаётся на паузе.
- Синтез по предложениям, звук — с первого готового.
- Выход стрелкой назад ставит свой текст на паузу; продолжает пользователь кнопкой play.

## Файлы

| Файл | Кто создаёт / кто вызывает | Назначение |
|---|---|---|
| `tts_player/synth_worker.py` | запускает `text_speaker.py` интерпретатором `claude-tts` | контракт (10), сторона синтеза |
| `tts_player/text_speaker.py` | создаёт `__main__.py`; вызывает `window.py` | запуск синтеза, игра, пауза главного |
| `tts_player/player.py` | `text_speaker.py` | `hold()` / `release()` главного плеера |
| `tts_player/window.py` | `__main__.py` | кнопка и экран «Свой текст» |

---

### Task 1: Процесс синтеза

**Goal:** `synth_worker.py` режет текст на предложения и пишет WAV по одному, сообщая о каждом строкой JSON.

**Files:**
- Create: `tts_player/synth_worker.py`
- Test: `tests/test_synth_worker.py`

**Contract:** контракт (10), сторона процесса синтеза

```
<python> tts_player/synth_worker.py <out_dir>        # out_dir создаётся
stdin    текст целиком, затем EOF
stdout   {"count": k}
         {"index": i, "path": "<out_dir>/<i>.wav", "duration": <с>}   i = 0..k-1, по порядку
         {"done": true}
         {"error": "<текст>"}          сбой синтеза предложения (False или исключение) — дальше не идёт, exit 1
exit     0 — всё; 1 — сбой синтеза; 2 — import claude_code_tts не удался ({"error": …} в stdout)
голос    load_raw_config()["active_persona"]
пустой текст / только пробелы → {"count": 0}, {"done": true}, exit 0
каждая строка stdout сбрасывается сразу (flush)
модуль импортирует только stdlib и claude_code_tts — не tts_player
```

**Acceptance Criteria:**
- [ ] с поддельным пакетом: три предложения → `count 3`, три строки с путями `0.wav`…`2.wav` и длительностью из WAV, `done`, exit 0
- [ ] поддельный `daemon_generate_speech` получает `active_persona` из поддельного `load_raw_config`
- [ ] сбой на втором предложении → строка `error`, третьего нет, exit 1
- [ ] без пакета в `PYTHONPATH` → `error`, exit 2
- [ ] пустой текст → `count 0`, `done`, exit 0
- [ ] первая строка с путём приходит в stdout раньше, чем синтезировано последнее предложение (поддельный синтез с задержкой)

**Verify:** `python3 -m unittest tests.test_synth_worker -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — поддельный `claude_code_tts` во временном каталоге.
- [ ] **Step 2: Реализация** — по контракту.
- [ ] **Step 3: Verify** — команда выше и полный `python3 -m unittest discover -s tests`.
- [ ] **Step 4: Commit** — `tts_player/synth_worker.py tests/test_synth_worker.py`.

### Task 2: `TextSpeaker` и пауза главного плеера

**Goal:** `TextSpeaker` синтезирует текст процессом из Task 1, играет готовые предложения подряд и держит главный плеер на паузе.

**Files:**
- Create: `tts_player/text_speaker.py`
- Modify: `tts_player/player.py`
- Test: `tests/test_text_speaker.py`, `tests/test_player.py`

**Contract:** `TextSpeaker` ↔ процесс синтеза и `Player`

```
Player.hold() -> None       # playing → paused; пока держит, сам не начинает ни следующее, ни новое
Player.release() -> None    # снимает удержание; статус не меняет (остаётся paused)

@dataclass(frozen=True)
class TextState: status: str            # "empty" | "synthesizing" | "playing" | "paused" | "stopped" | "error"
                 position_s: float; length_s: float; complete: bool; error: str

class TextSpeaker:
    def __init__(self, runtime_dir: Path, python: Path, worker: Path, player: Player,
                 audio_sink: str | None = None) -> None
    on_change: Callable[[TextState], None]
    def state(self) -> TextState
    def set_text(self, text: str) -> None    # отличается от прежнего → отмена синтеза, каталог удалён, status empty/stopped
    def toggle_play_pause(self) -> None
    def seek(self, position_s: float) -> None   # в пределах уже синтезированного
    def shutdown(self) -> None                  # отмена, каталог удалён, release()
    def cleanup_leftovers(self) -> None         # при старте: удалить <runtime>/text/*

toggle_play_pause:
  нет звука для текущего текста → запуск процесса (контракт (10)), status synthesizing → playing с первого готового
  playing → paused; paused → playing с места; stopped → playing с позиции (в конце → с 0)
  переход в playing → player.hold(); paused / stopped / конец → player.release()
длина  length_s — сумма duration пришедших; complete — после {"done"}
конец  последнее предложение доиграло и complete → stopped, позиция 0
       доиграло готовое, а синтез идёт → ждёт следующего (status playing)
ошибка {"error"} / exit ≠ 0 / процесс не запустился → status error, error = текст; готовое можно доиграть
каталог <runtime>/text/<n>/, n растёт; удаляется при set_text с другим текстом и в shutdown
```

**Acceptance Criteria:**
- [ ] `Player.hold()` из playing → paused; в hold конец сообщения и новое в idle не запускают воспроизведение; `release()` статус не меняет
- [ ] `TextSpeaker` с поддельным процессом (скрипт, отдающий строки контракта с задержками): play → `hold` вызван, первый файл играет до прихода последнего, длина растёт
- [ ] повторный play после конца того же текста — без нового процесса
- [ ] `set_text` с другим текстом во время синтеза — процесс получил SIGTERM, каталог удалён, следующий play запускает новый процесс
- [ ] `{"error"}` и exit 2 → `status error` с текстом; уже готовое предложение играет
- [ ] пауза и конец вызывают `release`; `shutdown` убивает процесс, удаляет каталог и вызывает `release`
- [ ] `cleanup_leftovers` удаляет `<runtime>/text/*` и не трогает `inbox/`

**Verify:** `python3 -m unittest tests.test_text_speaker tests.test_player -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — поддельный процесс синтеза — скрипт во временном каталоге; `Playback` — `fakesink`, как в существующих тестах.
- [ ] **Step 2: Реализация** — по контракту; чтение stdout процесса — неблокирующее, в главном цикле GLib.
- [ ] **Step 3: Verify** — команда выше и полный `python3 -m unittest discover -s tests`.
- [ ] **Step 4: Commit** — файлы задачи по путям.

### Task 3: Кнопка и экран «Свой текст»

**Goal:** кнопка `document-edit-symbolic` в ряду служебных кнопок открывает экран: поле ввода, ползунок, `[play|pause]`, стрелка назад.

**Files:**
- Modify: `tts_player/window.py`, `tts_player/__main__.py`, `README.md`
- Test: `tests/gui_window_cases.py`

**Contract:** окно ↔ `TextSpeaker` из Task 2

```
PlayerWindow(…, text_speaker: TextSpeaker)
  text_button: document-edit-symbolic, без текста, подсказка «Озвучить свой текст»; в ряду служебных кнопок слева, после voicing_off_button
  клик → стек на "text"
  экран "text": text_back_button (go-previous-symbolic) · text_view (Gtk.TextView, перенос по словам, растягивается)
                · text_scale (ползунок 0..length_s) · text_play_button (⏸ при playing/synthesizing, иначе ⏵) · text_position (время)
  изменение буфера text_view → text_speaker.set_text(текст)
  text_play_button → text_speaker.toggle_play_pause(); неактивна при пустом тексте
  text_scale, сдвинутый пользователем → text_speaker.seek(value)
  status error → текст ошибки под ползунком
  стрелка назад → главный экран; свой текст встаёт на паузу (playing/synthesizing → paused, синтез идёт дальше);
                 продолжает пользователь кнопкой play на экране «Свой текст»
__main__: TextSpeaker(<runtime>, Path("~/.local/share/uv/tools/claude-code-tts/bin/python").expanduser(),
          <пакет>/synth_worker.py, player); cleanup_leftovers() до start; shutdown() при выходе
          --tts-python FILE переопределяет интерпретатор
```

**Acceptance Criteria:**
- [ ] кнопка: иконка, подсказка, без текста, место в ряду служебных кнопок
- [ ] клик → экран `"text"`; стрелка → `"main"`, играющий свой текст встал на паузу
- [ ] ввод текста вызывает `set_text` с содержимым буфера; play — `toggle_play_pause`; пустой текст — play неактивна
- [ ] подпись play и ползунок следуют `TextState` (playing, paused, synthesizing, stopped, error)
- [ ] сдвиг ползунка пользователем вызывает `seek`; обновление из состояния — не вызывает
- [ ] README: экран, что дорожка не попадает в список, пауза главного плеера, `--tts-python`

**Verify:** `python3 -m unittest discover -s tests` → `OK`, GUI-случаи не пропущены; `pgrep broadwayd` пуст

**Steps:**

- [ ] **Step 1: Падающие GUI-случаи** — `FakeTextSpeaker`.
- [ ] **Step 2: Реализация** — по контракту; логику перетаскивания ползунка не дублировать с главным — общий помощник.
- [ ] **Step 3: `__main__.py` и README**.
- [ ] **Step 4: Verify** — команда выше, вывод — в отчёт.
- [ ] **Step 5: Commit** — файлы задачи по путям.
