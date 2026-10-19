# TTS Player на Linux и macOS: одна установка, штатные стыки

**Статус:** реализовано 2026-10-19 (срезы 1–5); на macOS не проверено — первый прогон у друга.

## Задача

Со слов пользователя (2026-10-19): поделиться плеером с другом на macOS. Проверить, что нужно
доработать, чтобы установка на Linux и macOS была одинаковой; сделать `install.sh` для обеих ОС;
найти костыли — стыки компонентов, сделанные обходным путём, — и привести их к штатным способам
интеграции.

## Как плеер подключён сейчас

```
Claude Code ──Stop/PostToolUse──> ~/.claude/hooks/tts-gate.sh (вне репозитория)
   └─> speak-response.sh: HOME=hook-home claude-tts speak --from-hook
         └─> hook-home/.claude-tts/queue/   (фальшивый HOME, heartbeat — копия от плеера)
               └─> tts-player: текст сообщения, задача в ~/.claude-tts/queue
                     └─> claude-tts daemon (PATH: daemon-bin первым)
                           └─> daemon-bin/paplay (подмена системного) ─> inbox/ ─> tts-player
Claude Code ──SessionStart/End──> tts-player-hook (bash): /proc, flock, setsid, jq
```

## Аудит: что держится на обходе и что ломается на macOS

| # | Сейчас | Почему костыль | На macOS | Штатный способ |
|---|---|---|---|---|
| 1 | `daemon-bin/paplay` — подмена системного проигрывателя через `PATH` демона | демон не знает о плеере; держится на порядке поиска в `detect_player()` и на том, что демон запущен с нужным `PATH` | демон ищет сначала `afplay`, `paplay` там нет — обёртку не найдёт никогда | в форке claude-tts — настройка `player_command` в `config.json`: демон зовёт её вместо `detect_player()` |
| 2 | обёртка берёт id задачи из `~/.claude-tts/playback.json` через `jq` | угадывает текущую задачу по чужому файлу состояния | `date +%s%N` в BSD `date` нет — имя файла ломается | демон передаёт id сообщения в окружении команды (`CLAUDE_TTS_MESSAGE_ID`); приёмник — подкоманда `tts-player sink` на Python, время — `time.time_ns()` |
| 3 | `tts-player-hook start` читает `/proc/<pid>/environ`, чтобы перезапустить демон с `daemon-bin` первым в `PATH` | следствие п. 1 | `/proc` нет | уходит вместе с п. 1: `player_command` читается из конфига, перезапуск не нужен |
| 4 | `hook-home`: фальшивый HOME для `claude-tts speak --from-hook`, ссылки на `config.json` и `sessions.d/`, heartbeat-копия раз в 10 с (`hook_home.py`) | перехват очереди хука через подмену домашнего каталога | работает, но хрупко так же | в форке — переменная `CLAUDE_TTS_QUEUE_DIR`: куда `speak` кладёт задачу. Heartbeat-копия не нужна: «плеер не запущен — молчать» уже решает гейт (`tts-player session has`), «демон мёртв — молчать» — сам `speak` по настоящему heartbeat |
| 5 | хуки речи и гейт — файлы в `~/.claude/hooks/` вне репозитория, правятся руками по README (`tts-gate.sh`, `speak-response.sh`, `speak-intermediate.sh`) | установку не повторить на другой машине; правки README «поменяйте строку запуска» | то же | все события Claude Code — подкоманды `tts-player-hook` в репозитории: `start`, `end`, `stop`, `post-tool-use`, `prompt`; `install.sh` вписывает их в `~/.claude/settings.json` |
| 6 | `~/.claude/hooks/bin/paplay` и `bin/piper` — обёртки режима `direct` | в режиме `queue` синтезирует демон, его `PATH` этих обёрток не содержит — мёртвый код | — | не переносятся; `install.sh` их не ставит |
| 7 | `tts-player-hook` на bash: `flock`, `setsid`, `jq`, `/proc` | — | `flock` и `setsid` в macOS нет, `jq` не стоит по умолчанию | хук — лёгкий Python-модуль, как `session_command`: проба блокировки — `Instance` (`fcntl`), отвязка — `subprocess.Popen(start_new_session=True)`, JSON — `json` |
| 8 | интерпретатор claude-tts для воркеров — `~/.local/share/uv/tools/claude-code-tts/bin/python` по умолчанию в `--tts-python` | путь установки uv зашит | у uv на macOS тот же путь, но `UV_TOOL_DIR` его меняет | интерпретатор — из shebang `claude-tts` в `PATH` (так его нашёл бы любой запуск `claude-tts`) |
| 9 | `XDG_RUNTIME_DIR` обязателен (плеер, хук, обёртка) | — | в macOS переменной нет — плеер не стартует | одна функция каталога состояния: `XDG_RUNTIME_DIR`, на macOS — `$TMPDIR` (личный для пользователя, чистится перезагрузкой, как `/run/user`) |
| 10 | трей — `XApp.StatusIcon` | выбран под Cinnamon (спека 2026-10-09) | XApp нет | XApp, если библиотека есть; иначе `Gtk.StatusIcon` (в GTK 3 для macOS он рисуется в строке меню) |
| 11 | пауза других плееров — MPRIS по D-Bus | штатно для Linux | сессионной шины нет; сейчас — строка в лог, флажок в настройках ничего не делает | на macOS флажок «ставить на паузу другие плееры» скрыт |
| 12 | ожидание диктовки — `pactl subscribe` + `/proc/<pid>/exe` | штатно для PipeWire | `pactl` нет; сейчас — строка в лог | на macOS настройка паузы на диктовку скрыта |
| 13 | установка — `ln -s` по README, claude-tts из `git+file://~/Programs/claude-code-tts` | шаги руками, путь к клону автора | — | `install.sh`, claude-tts — из `git+https://github.com/lav45/claude-code-tts@lav45` (форк публичный, ветка `lav45` в origin совпадает с локальной) |
| 14 | в `settings.json` абсолютные `/home/lav45/...` | — | у друга другой HOME | `install.sh` пишет пути своей машины |

Плеер в целом переносим: GTK 3, GStreamer `playbin`/`autoaudiosink` и PyGObject есть в Homebrew
(`gtk+3`, `gstreamer`, `pygobject3`), блокировка экземпляра — `fcntl.flock` (есть в macOS),
`OutputIdle` (тишина перед звуком для уснувшего вывода) на macOS безвреден.

Решение 2026-10-09 «Python 3 + PyGObject из системных пакетов, без venv и pip» остаётся: на macOS
`pygobject3` из Homebrew ставится в Python Homebrew, а он стоит первым в `PATH` при штатной
настройке brew. `install.sh` проверяет `python3 -c 'import gi'` и при сбое называет причину.

## `install.sh`

Один скрипт в корне репозитория, повторный запуск — обновление (идемпотентен).

1. ОС: `uname -s` → `Linux` | `Darwin`; иначе выход с сообщением.
2. Системные пакеты:
   - Linux (apt): `python3-gi gir1.2-gtk-3.0 gir1.2-gstreamer-1.0 gstreamer1.0-plugins-good
     ffmpeg`, `gir1.2-xapp-1.0` — если пакет есть в репозитории, `pulseaudio-utils` (`pactl`);
     другие дистрибутивы — список пакетов в сообщении, без установки;
   - macOS (brew): `pygobject3 gtk+3 gstreamer adwaita-icon-theme ffmpeg`; нет brew — выход с
     ссылкой на brew.sh.
3. `uv` — если нет, официальный установщик astral.
4. claude-tts из форка: `uv tool install --force "claude-code-tts @ <источник>"`, источник по
   умолчанию — GitHub-форк, переопределяется аргументом (`--claude-tts-source git+file://…`
   для локального клона).
5. Голос: `mode: queue` в `~/.claude-tts/config.json`; модель Supertonic через штатный
   установщик claude-tts (уточнить команду при реализации).
6. `~/.local/bin/tts-player`, `~/.local/bin/tts-player-hook` — ссылки на `bin/` репозитория.
7. `~/.claude/settings.json`: копия `settings.json.bak-<дата>`, затем записи хуков
   `SessionStart`, `SessionEnd`, `Stop`, `PostToolUse`, `UserPromptSubmit` →
   `tts-player-hook <событие>`. Прежние записи плеера и claude-tts (`tts-gate.sh …`,
   `speak-*.sh`) заменяются, чужие записи (например `rtk hook claude`) не трогаются.
8. Перезапуск демона (`claude-tts daemon restart`) — чтобы прочитал `player_command`.
9. Проверка в конце: `import gi, Gtk, Gst`, `claude-tts --version`, демон жив.

## Срезы

Каждый срез — законченное состояние, плеер на Linux после него работает.

1. **Форк claude-tts:** `player_command` + `CLAUDE_TTS_MESSAGE_ID`, `CLAUDE_TTS_QUEUE_DIR`;
   тесты форка. Обе правки общего вида — годятся в upstream.
2. **Плеер:** `tts-player sink` вместо `daemon-bin/paplay`; `hook-home` и heartbeat-копия
   удалены, плеер слушает свой каталог очереди хуков; каталог состояния с запасным `$TMPDIR`;
   интерпретатор claude-tts из shebang.
3. **Хук на Python:** все события Claude Code в `tts-player-hook`, гейт внутри; файлы
   `~/.claude/hooks/tts-gate.sh`, `speak-*.sh` больше не нужны.
4. **macOS в интерфейсе:** трей с запасным `Gtk.StatusIcon`; настройки MPRIS и диктовки
   скрыты там, где механизма нет.
5. **`install.sh` и README:** README — установка одной командой, разделы о ручных переходах
   2026-10-09 уходят. Переход машины пользователя — запуском того же `install.sh`.

## Ограничения

- На macOS ничего не проверено: машины с macOS нет. Первый прогон у друга — проверка; что
  упадёт, видно в `player.log` и выводе `install.sh`.
- Форк нужно отправить в GitHub после среза 1 (`git push origin lav45`), иначе `install.sh`
  у друга поставит claude-tts без `player_command`.

## Открытые вопросы

- Как ведёт себя `Gtk.StatusIcon` в строке меню macOS (левый клик, меню) — проверяется только
  у друга.
- Supertonic на macOS: ставится ли sherpa-onnx тем же установщиком claude-tts — проверить по
  коду форка в срезе 5.
