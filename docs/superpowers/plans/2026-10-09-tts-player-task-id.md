# TTS Player — id задачи в имени файла предложения. План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** обёртка `paplay` метит каждый файл предложения id задачи демона, и плеер собирает
предложения в сообщение по этому id, а не по случайному токену прохода синтеза.

**Architecture:** `daemon-bin/paplay` читает `current_message.id` из `~/.claude-tts/playback.json`
(`jq`) и вставляет его в имя файла в `inbox/`. `tts_player/inbox.py` разбирает новое имя,
`Sentence` несёт `task_id`, `MessageQueue` группирует по `task_id`; при `unknown` — по
`(session_id, token)`, как сейчас.

**Tech Stack:** bash + `jq` (уже используется в `~/.claude/hooks/tts-toggle.sh`), Python 3.12 + системный
PyGObject — как в `master`. Новых зависимостей нет.

**Slice:** контракт (13) «обёртка `paplay` → плеер: файл с id задачи» — первым из спеки кэша:
стоит сам на нынешнем входе (хук пишет очередь демона напрямую, id задачи — id сообщения
`claude-tts`) и даёт плееру точную границу сообщения, на которой строятся (11) и (12).

**Next slices (not planned here):** контракт (11) «хук → плеер: задача хука» (подменённый HOME,
хранение текста); контракт (12) «плеер → демон: задача демону» (очередь пишет только плеер,
переозвучка при проигрывании).

**Spec:** `docs/superpowers/specs/2026-10-09-tts-player-text-cache-design.md`

**Assumptions:**
- Демон пишет `current_message` (запись сообщения с `id`, 16 hex — `daemon.py:1014`) в
  `~/.claude-tts/playback.json` до синтеза предложений сообщения (`stream_message`,
  `daemon.py:812`) и сбрасывает в `null` после (`clear_current_message`, `daemon.py:276`) —
  проверено по коду 9.20.1. Обёртку `paplay` демон вызывает, пока сообщение текущее.
- Повторный проход синтеза одного сообщения (пауза или перезапуск демона посреди сообщения)
  получает новый `token` (`daemon.py:692`), но тот же `id` — по коду; поэтому группировка по
  `id` склеивает проходы в одно сообщение.
- `jq` 1.7 есть в системе (`/usr/bin/jq`) — проверено.

## Global Constraints

- Контракт (13) из спеки — дословно: `inbox/<recv_ns>_<id>_<basename>`; нет файла / поля /
  не JSON → `unknown`; запись атомарная через `.part` + `mv`; сбой — stderr и
  `logger -t tts-player-paplay`.
- Пока нет среза (12), id задачи плееру заранее не известен: файл с любым id принимается;
  правило «чужой id → удалить» из (13) включает срез (12).
- Тесты не трогают настоящий `~/.claude-tts/playback.json`: обёртка в тестах получает HOME во
  временном каталоге.
- Коммит по путям, без `Co-Authored-By`.

**User decisions (already made):**
- Демон под управлением плеера; в очередь демона в итоге пишет только плеер (срез (12)).
- Звук демона идёт через обёртку `paplay` в буфер плеера (перехват звука остаётся).

## Файлы

| Файл | Кто вызывает | Назначение |
|---|---|---|
| `daemon-bin/paplay` | демон `claude-tts` по `PATH` | id задачи в имени файла |
| `tts_player/inbox.py`, `tts_player/model.py` | `player.py` | разбор имени, `Sentence.task_id` |
| `tts_player/queue.py` | `player.py` | группировка по `task_id` |
| `tests/test_paplay_wrapper.py`, `tests/test_inbox.py`, `tests/test_queue.py`, `tests/test_player_e2e.py` | `unittest discover` | проверки |

---

### Task 1: Обёртка пишет id задачи в имя файла

**Goal:** `daemon-bin/paplay` кладёт файл как `inbox/<recv_ns>_<id>_<basename>`.

**Files:**
- Modify: `daemon-bin/paplay`
- Test: `tests/test_paplay_wrapper.py`

**Contract:** контракт (13), сторона обёртки

```
daemon-bin/paplay <опции> <wav>
  id = current_message.id из $HOME/.claude-tts/playback.json, если это [0-9a-f]{1,64};
       нет файла / поля / null / не JSON / другой вид → "unknown"
  inbox/<recv_ns>_<id>_<basename(wav)>     атомарно: .<имя>.part, затем mv (как сейчас)
  объявления (tts_announce.wav, tts_daemon_announce.wav) — пропуск (как сейчас)
  сбой → stderr и logger -t tts-player-paplay, exit 1 (как сейчас)
```

**Acceptance Criteria:**
- [ ] `playback.json` с `current_message.id = "a1b2c3d4e5f60718"` → файл `inbox/<19 цифр>_a1b2c3d4e5f60718_tts_queue_S_tok_s0.wav`
- [ ] нет `playback.json`, `current_message: null`, битый JSON, id с недопустимыми символами → в имени `unknown`
- [ ] прежние проверки обёртки (атомарность, объявления, ошибки, journal) проходят

**Verify:** `python3 -m unittest discover -s tests -p 'test_paplay_wrapper.py' -v` → `OK`

**Steps:**

- [ ] **Step 1: Падающие тесты** — HOME во временном каталоге со своим `playback.json`.
- [ ] **Step 2: Реализация** — по контракту; id проверяется регулярным выражением до вставки в имя.
- [ ] **Step 3: Verify** — команда выше и полный `python3 -m unittest discover -s tests`.
- [ ] **Step 4: Commit** — `daemon-bin/paplay tests/test_paplay_wrapper.py`.

### Task 2: Плеер собирает сообщение по id задачи

**Goal:** предложения с одним `task_id` — одно сообщение, даже если демон синтезировал их в два прохода.

**Files:**
- Modify: `tts_player/inbox.py`, `tts_player/model.py`, `tts_player/queue.py`, `tts_player/player.py` (только журнальные строки)
- Test: `tests/test_inbox.py`, `tests/test_queue.py`, `tests/test_player_e2e.py`

**Contract:** контракт (13), сторона плеера

```
имя в inbox/: <recv_ns:19 цифр>_<task_id>_tts_queue_<session_id>_<token>_s<index>.wav
              task_id = [0-9a-f]{1,64} | unknown
Sentence: recv_ns, task_id, session_id, token, index, path
Message:  task_id (вместо пары session_id+token как ключа), session_id, token (первого прохода), …
MessageQueue: ключ сообщения — task_id; task_id == "unknown" → ключ (session_id, token), как сейчас
              «более позднее сообщение» (is_complete) — сообщение с другим ключом
имя старого вида (без task_id) → остаётся в inbox, WARNING (как любое неподходящее имя)
```

**Acceptance Criteria:**
- [ ] `inbox` разбирает новое имя; `Sentence.task_id` заполнен; имя без task_id — WARNING, файл в inbox
- [ ] два прохода одного task_id с разными token → одно сообщение, предложения по recv_ns
- [ ] разные task_id одной сессии → два сообщения; первое complete после появления второго
- [ ] task_id `unknown` — группировка по (session_id, token), прежние тесты очереди проходят
- [ ] вытеснение, `clear`, `release`/`put_back`, склейка (attach) работают по новому ключу; `put_back` возвращает файл с тем же именем
- [ ] e2e: обёртка с `playback.json` (id = X) кладёт два прохода с разными token, плеер играет их как одно сообщение (одна строка `complete`)

**Verify:** `python3 -m unittest discover -s tests` → `OK`, GUI-случаи не пропущены

**Steps:**

- [ ] **Step 1: Падающие тесты** — inbox, очередь, e2e с настоящей обёрткой.
- [ ] **Step 2: Реализация** — по контракту; ключ сообщения — одна функция, все места (`_find`, `_evicted`, `clear`) зовут её.
- [ ] **Step 3: Verify** — команда выше, вывод — в отчёт.
- [ ] **Step 4: Commit** — файлы задачи по путям.
