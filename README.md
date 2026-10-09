# TTS Player

Плеер озвучки Claude Code для Linux: окно в трее с очередью и историей озвученных
реплик, кнопками паузы и пропуска. Проигрывает звук, который синтезирует демон
`claude-tts` в режиме `queue`.

Дизайн: [docs/superpowers/specs/2026-10-09-tts-player-design.md](docs/superpowers/specs/2026-10-09-tts-player-design.md).

## Как подключается

```
claude-tts daemon -> daemon-bin/paplay (обёртка) -> буфер плеера -> tts-player (трей, история)
```

Демон ищет проигрыватель `paplay` через `PATH`. `bin/tts-player-hook` запускает демон
с `daemon-bin/` первым в `PATH` (или перезапускает, если он работает без обёртки), поэтому
вместо системного `paplay` звук попадает в буфер плеера. Плеер хранит историю реплик.

## Установка

```bash
ln -s ~/Programs/TTSReader/bin/tts-player ~/.local/bin/tts-player
ln -s ~/Programs/TTSReader/bin/tts-player-hook ~/.local/bin/tts-player-hook
```

В `~/.claude/hooks/tts-toggle.sh`:

- ветка `on`, режим `queue`: вместо `claude-tts daemon start/status` одна строка `tts-player-hook on`;
- ветка `off`, когда флаг этой сессии снят: `tts-player-hook off`.

## Команды

- `tts-player --tray` - плеер в трее (так его запускает хук);
- `tts-player --show` - показать окно;
- `tts-player --no-gui` - без окна, только приём и воспроизведение;
- `tts-player stop` - остановить текущий звук, плеер остаётся;
- `/tts-on`, `/tts-off` - включить и выключить озвучку сессии (хук зовёт `tts-player-hook on|off`);
- меню трея «Выход» - закрыть плеер.

## Тесты

```bash
python3 -m unittest discover -s tests
```

Случаи с окном автоматически идут под `broadwayd`.

## Проверка вручную

```bash
python3 -m unittest discover -s tests
command -v tts-player tts-player-hook
# в сессии Claude Code:
/tts-on     # демон перезапущен с обёрткой, плеер в трее
/tts-off    # звук остановлен
```

Если демон уже работал без обёртки, перезапуск занимает до 15 с.

## Логи

- `$XDG_RUNTIME_DIR/tts-player/player.log` - вывод плеера;
- `$XDG_RUNTIME_DIR/tts-player/daemon-restart.log` - перезапуск демона;
- `journalctl -t tts-player-paplay` - обёртка `paplay`;
- `~/.claude-tts/daemon.log` - демон.
