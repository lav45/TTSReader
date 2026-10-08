# TTS Player — буфер предложений: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** демон `claude-tts` отдаёт каждое синтезированное предложение не в динамик, а в
буфер `$XDG_RUNTIME_DIR/tts-player/inbox/`, откуда его заберёт плеер.

**Architecture:** обёртка `paplay` на bash лежит в собственном каталоге `daemon-bin/`,
который ставится первым в `PATH` демона; `detect_player()` демона находит её вместо
системной. Обёртка атомарно кладёт копию WAV в буфер и сразу завершается. Читающая
сторона в этом срезе — заглушка в тестах, читающая буфер по контракту.

**Tech Stack:** bash + coreutils (`cp`, `mv`, `date +%s%N`) для обёртки — как соседние
обёртки `~/.claude/hooks/bin/`; тесты — `unittest` из стандартной библиотеки Python 3.12
(`bats`, `shellcheck` в системе нет, новых зависимостей срез не вносит).

**Slice:** контракт (3) «Обёртка paplay → плеер (буфер)» — пишущая сторона опирается
только на существующий демон, а буфер — вход для всего плеера и для контракта (4).

**Next slices (not planned here):**
- (3), читающая сторона в плеере: забор из `inbox`, очередь, воспроизведение с перемоткой.
- (2) История демона → плеер: признак конца сообщения, замена предложений склейкой, хвост из 5.
- (4) `tts-toggle.sh` → плеер: `tts-player --tray`, `tts-player stop`, демон с `daemon-bin/` в `PATH`.

**Spec:** `docs/superpowers/specs/2026-10-09-tts-player-design.md`

**Assumptions:**
- Демон находит плеер по `PATH` своего процесса: `detect_player()` возвращает имя
  `["paplay"]`, `Popen` ищет его по `PATH`. Проверено 2026-10-09 под Python демона
  (`~/.local/share/uv/tools/claude-code-tts/bin/python -I`): при временном каталоге первым
  в `PATH` `shutil.which("paplay")` указал на заглушку.
- У работающего демона есть `XDG_RUNTIME_DIR=/run/user/1000` — проверено по
  `/proc/<pid>/environ`.
- Демон не читает код возврата плеера и удаляет файлы предложений только после склейки
  (`daemon.py:757`, `:883`) — прочитано в коде; синхронный `cp` в обёртке успевает.
- `date +%s%N` даёт 19 цифр до 2286 года — сортировка имён по строке совпадает с порядком
  приёма; не риск.

## Global Constraints

- Обёртка никогда не играет звук и не вызывает системный `paplay`.
- Обёртка не трогает файлы вне `$XDG_RUNTIME_DIR/tts-player/inbox/` и не удаляет исходный WAV.
- Тесты не трогают работающий демон, `~/.claude-tts/` и настоящий `$XDG_RUNTIME_DIR`:
  каждый тест задаёт свой временный `XDG_RUNTIME_DIR`.
- Коммит — только по путям задачи (`git commit <пути> -m …`), без `Co-Authored-By`.

**User decisions (already made):**
- Обёртка только кладёт файл в буфер; звук сама не играет ни при каких условиях.
- Буфер в tmpfs (`$XDG_RUNTIME_DIR`); при выходе из системы стирается.
- Объявления демона (`tts_announce.wav`, `tts_daemon_announce.wav`) в буфер не попадают.
- Обёртке — свой каталог, не `~/.claude/hooks/bin/` (там flock-обёртка `paplay` режима direct).

---

### Task 1: Обёртка paplay кладёт предложение в буфер

**Goal:** `daemon-bin/paplay` выполняет контракт (3): атомарно кладёт копию WAV в буфер и
завершается, объявления пропускает.

**Files:**
- Create: `daemon-bin/paplay` (исполняемый)
- Test: `tests/test_paplay_wrapper.py`

**Contract:** контракт (3) из спеки — пишущая сторона

```
paplay [опции] <wav>                      # опции не используются; <wav> — последний аргумент

буфер:  $XDG_RUNTIME_DIR/tts-player/inbox/            # создаётся, если нет
файл:   {recv_ns}_{basename(<wav>)}                   # recv_ns = date +%s%N, 19 цифр
запись: cp <wav> inbox/.{имя}.part  →  mv inbox/.{имя}.part inbox/{имя}

basename(<wav>) ∈ {tts_announce.wav, tts_daemon_announce.wav} → exit 0, буфер не тронут
exit 0 — файл в буфере
exit 1 — нет аргумента, нет XDG_RUNTIME_DIR, копия не удалась; сообщение в stderr,
         в буфере не остаётся ни файла, ни .part
```

**Acceptance Criteria:**
- [ ] вызов с `/…/tts_queue_S_T_s0.wav` даёт ровно один файл `inbox/<19 цифр>_tts_queue_S_T_s0.wav`, побайтно равный исходному, и exit 0
- [ ] после успешного вызова в `inbox` нет файлов с точкой в начале имени
- [ ] опции перед файлом (`paplay --volume=1 x.wav`) не мешают: в буфер попадает `x.wav`
- [ ] три последовательных вызова дают имена, сортировка которых по строке совпадает с порядком вызовов
- [ ] `tts_announce.wav` и `tts_daemon_announce.wav` → exit 0, `inbox` пуст
- [ ] несуществующий исходный файл → exit 1, непустой stderr, `inbox` без файлов и `.part`
- [ ] без `XDG_RUNTIME_DIR` → exit 1, непустой stderr
- [ ] исходный WAV после вызова на месте

**Verify:** `python3 -m unittest discover -s tests -p 'test_paplay_wrapper.py' -v` → `OK`, 8 тестов

**Steps:**

- [ ] **Step 1: Падающие тесты** — по одному тесту на критерий; обёртку зовут через `subprocess` с временным `XDG_RUNTIME_DIR`. Падать должны оттого, что обёртки нет, а не от опечатки.
- [ ] **Step 2: Обёртка** — по контракту выше; шапка-комментарий в стиле `~/.claude/hooks/bin/paplay`: зачем обёртка, кто её находит (демон по `PATH`), куда кладёт.
- [ ] **Step 3: Verify** — команда выше, вывод — в отчёт задачи.
- [ ] **Step 4: Commit** — `daemon-bin/paplay tests/test_paplay_wrapper.py`.

### Task 2: Перехват через PATH демона — приёмка через стык

**Goal:** доказать стык целиком: плеер, найденный кодом демона при `daemon-bin/` первым в
`PATH`, — наша обёртка, и предложения сообщения доходят до читающей стороны по порядку.

**Files:**
- Test: `tests/test_daemon_interception.py`

**Contract:** контракт (3) из спеки — читающая сторона, заглушка

```
читатель-заглушка:  sorted(f for f in inbox/ if not f.startswith("."))
предложение:        {recv_ns}_tts_queue_{session_id}_{token}_s{i}.wav

вызов как у демона (daemon_play_audio, daemon.py:532):
  cmd = detect_player() + [str(wav)]      # под Python демона, PATH = daemon-bin/:$PATH
  subprocess.Popen(cmd).wait()
```

**Acceptance Criteria:**
- [ ] под `~/.local/share/uv/tools/claude-code-tts/bin/python -I` с `daemon-bin/` первым в `PATH` `shutil.which(detect_player()[0])` — это `daemon-bin/paplay`
- [ ] три предложения `s0, s1, s2` одного `token`, отданные вызовом как у демона, читатель-заглушка получает в порядке `s0, s1, s2`, каждое побайтно равно исходному
- [ ] объявление, отданное тем же путём между `s0` и `s1`, читатель не видит
- [ ] тест пропускается (`skipTest`) с понятной причиной, если Python демона не найден

**Verify:** `python3 -m unittest discover -s tests -v` → `OK`, все тесты обеих задач

**Steps:**

- [ ] **Step 1: Падающая приёмка** — сначала проверка `which`; вызов демона — подпроцессом под его Python, не импортом `daemon_play_audio` (он пишет состояние живого демона в `~/.claude-tts/`).
- [ ] **Step 2: Довести до зелёного** — если падает, причина в обёртке или `PATH`, а не в тесте; правка обёртки — по контракту Task 1.
- [ ] **Step 3: Verify** — команда выше, вывод — в отчёт задачи.
- [ ] **Step 4: Commit** — `tests/test_daemon_interception.py` (и `daemon-bin/paplay`, если менялась).
