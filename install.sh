#!/usr/bin/env bash
#
# install.sh — установка и обновление TTS Player на Linux (apt) и macOS (Homebrew).
#
# Запуск из клона репозитория: ./install.sh [--claude-tts-source <источник uv>]
# Повторный запуск — обновление: каждый шаг сначала смотрит, сделан ли он.
#
#  1. системные пакеты: GTK 3, GStreamer, PyGObject, ffmpeg;
#  2. Python с PyGObject — его путь пишется в .python репозитория (читают bin/tts-player*);
#  3. uv и claude-tts из форка (по умолчанию CLAUDE_TTS_SOURCE ниже);
#  4. на чистой машине — штатный установщик claude-tts (Piper, голос по умолчанию, config.json);
#     затем движок sherpa-onnx и модель Supertonic 3;
#  5. ~/.claude-tts/config.json: mode queue, player_command, персона Supertonic;
#  6. ссылки ~/.local/bin/tts-player и tts-player-hook;
#  7. хуки в ~/.claude/settings.json (копия — settings.json.bak-<дата>);
#  8. запущенные демон и плеер перезапускаются — с новым кодом.
#
# Сбой шага — сообщение и выход 1; сделанные шаги остаются, повторный запуск их пропустит.

set -euo pipefail

CLAUDE_TTS_SOURCE="git+https://github.com/lav45/claude-code-tts@lav45"
SUPERTONIC_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/sherpa-onnx-supertonic-3-tts-int8-2026-05-11.tar.bz2"
SUPERTONIC_SHA256="82fa96f91c4ef8abaae3a14a3f4153facf88bed821d1f7331cec2700f432c427"
APT_PACKAGES=(python3-gi gir1.2-gtk-3.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good ffmpeg pulseaudio-utils curl)
APT_OPTIONAL=(gir1.2-xapp-1.0)  # значок трея XApp; без него — Gtk.StatusIcon
BREW_PACKAGES=(pygobject3 gtk+3 gstreamer adwaita-icon-theme ffmpeg)

REPO="$(cd "$(dirname "$0")" && pwd -P)"
ORIGINAL_PATH="$PATH"  # PATH пользователя: шаг uv дописывает в PATH скрипта ~/.local/bin
BIN_DIR="$HOME/.local/bin"
MODELS_DIR="$HOME/.claude-tts/sherpa-models"

step() { printf '\n==> %s\n' "$*"; }
fail() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --claude-tts-source) [ "$#" -ge 2 ] || fail "--claude-tts-source без значения"
            CLAUDE_TTS_SOURCE="$2"; shift 2 ;;
        -h | --help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) fail "неизвестный аргумент: $1 (см. --help)" ;;
    esac
done

OS="$(uname -s)"
case "$OS" in
    Linux | Darwin) ;;
    *) fail "ОС $OS не поддерживается: только Linux и macOS" ;;
esac

install_linux_packages() {
    if ! command -v apt-get > /dev/null; then
        echo "apt-get нет: поставьте пакетным менеджером дистрибутива аналоги ${APT_PACKAGES[*]}"
        return 0
    fi
    local missing=() package
    for package in "${APT_PACKAGES[@]}"; do
        dpkg -s "$package" > /dev/null 2>&1 || missing+=("$package")
    done
    for package in "${APT_OPTIONAL[@]}"; do
        if ! dpkg -s "$package" > /dev/null 2>&1 && apt-cache show "$package" > /dev/null 2>&1; then
            missing+=("$package")
        fi
    done
    if [ "${#missing[@]}" -eq 0 ]; then
        echo "пакеты уже стоят"
        return 0
    fi
    echo "ставлю: ${missing[*]}"
    sudo apt-get install -y "${missing[@]}"
}

install_macos_packages() {
    command -v brew > /dev/null || fail "нет Homebrew: поставьте его по https://brew.sh и запустите снова"
    local missing=() package
    for package in "${BREW_PACKAGES[@]}"; do
        brew list --formula "$package" > /dev/null 2>&1 || missing+=("$package")
    done
    if [ "${#missing[@]}" -eq 0 ]; then
        echo "пакеты уже стоят"
        return 0
    fi
    echo "ставлю: ${missing[*]}"
    brew install "${missing[@]}"
}

# Python 3.10+ (код плеера пишет типы как `str | None`), в котором есть PyGObject с GTK 3 и
# GStreamer: apt ставит его для системного python3, brew — для своего python3.N, под который
# собран pygobject3 (он может не совпадать с python3 brew по умолчанию).
find_python() {
    local candidates=() python
    if [ "$OS" = Darwin ]; then
        candidates+=("$(brew --prefix)/bin/python3")
        for python in "$(brew --prefix)"/bin/python3.[0-9]*; do
            [[ "$python" == *-config ]] || candidates+=("$python")
        done
    else
        candidates+=(/usr/bin/python3)
    fi
    candidates+=("$(command -v python3 || true)")
    for python in "${candidates[@]}"; do
        [ -n "$python" ] && [ -x "$python" ] || continue
        if "$python" -c 'import sys; assert sys.version_info >= (3, 10)
import gi; gi.require_version("Gtk", "3.0"); gi.require_version("Gst", "1.0")
from gi.repository import Gtk, Gst' > /dev/null 2>&1; then
            echo "$python"
            return 0
        fi
    done
    return 1
}

sha256_of() {
    "$PYTHON" -c 'import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
print(h.hexdigest())' "$1"
}

# Подоболочка: временный каталог убирает её же trap EXIT при любом исходе шага.
install_supertonic() (
    target="$MODELS_DIR/supertonic-3"
    if [ -f "$target/tts.json" ] && [ -f "$target/voice.bin" ]; then
        echo "модель уже в $target"
        exit 0
    fi
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    echo "скачиваю модель Supertonic 3 (~130 МБ)"
    curl -fL --progress-bar -o "$tmp/model.tar.bz2" "$SUPERTONIC_URL"
    [ "$(sha256_of "$tmp/model.tar.bz2")" = "$SUPERTONIC_SHA256" ] || fail "контрольная сумма модели не совпала"
    tar -xjf "$tmp/model.tar.bz2" -C "$tmp"
    mkdir -p "$MODELS_DIR"
    rm -rf "$target"
    mv "$tmp"/sherpa-onnx-supertonic-3-tts-int8-*/ "$target"
    echo "модель — в $target"
)

player_module() {
    PYTHONPATH="$REPO" "$PYTHON" -m "$@"
}

# PID запущенного плеера (держит блокировку экземпляра) или пусто.
player_pid() {
    PYTHONPATH="$REPO" "$PYTHON" -c 'from tts_player.instance import Instance, is_locked
from tts_player.runtime_dir import runtime_dir
runtime = runtime_dir()
if runtime is not None and is_locked(runtime):
    print(Instance.holder_pid(runtime) or "")'
}

state_dir() {
    PYTHONPATH="$REPO" "$PYTHON" -c 'from tts_player.runtime_dir import APP_DIR, runtime_dir
print(runtime_dir() / APP_DIR)'
}

step "Системные пакеты"
if [ "$OS" = Darwin ]; then install_macos_packages; else install_linux_packages; fi

step "Python с PyGObject"
PYTHON="$(find_python)" || fail "не нашёл python3 3.10+ с PyGObject, GTK 3 и GStreamer (import gi не прошёл)"
echo "$PYTHON" > "$REPO/.python"
echo "$PYTHON — записан в $REPO/.python"

step "uv"
export PATH="$BIN_DIR:$PATH"
if ! command -v uv > /dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
command -v uv > /dev/null || fail "uv не установился"
uv --version

step "claude-tts из $CLAUDE_TTS_SOURCE"
uv tool install --force "claude-code-tts @ $CLAUDE_TTS_SOURCE"
claude-tts --version

step "Установщик claude-tts"
if [ -f "$HOME/.claude-tts/config.json" ]; then
    echo "claude-tts уже настроен (~/.claude-tts/config.json есть)"
else
    claude-tts-install --install --yes --no-daemon-restart
fi
claude-tts-install --enable-sherpa --yes

step "Модель Supertonic 3"
install_supertonic

step "Ссылки в $BIN_DIR"
mkdir -p "$BIN_DIR"
ln -sfn "$REPO/bin/tts-player" "$BIN_DIR/tts-player"
ln -sfn "$REPO/bin/tts-player-hook" "$BIN_DIR/tts-player-hook"
echo "$BIN_DIR/tts-player, $BIN_DIR/tts-player-hook"

step "Настройки claude-tts"
player_module tts_player.claude_tts_config "$BIN_DIR/tts-player"

step "Хуки Claude Code"
player_module tts_player.claude_settings "$BIN_DIR/tts-player-hook"

step "Перезапуск запущенных демона и плеера"
if [ -f "$HOME/.claude-tts/daemon.pid" ] && kill -0 "$(cat "$HOME/.claude-tts/daemon.pid")" 2> /dev/null; then
    # демон уходит в фон с потоками вывода команды: в канал install.sh они попасть не должны,
    # иначе тот, кто читает вывод скрипта, ждёт конца канала, пока жив демон
    claude-tts daemon restart < /dev/null > "$HOME/.claude-tts/daemon-restart.log" 2>&1
    tail -n 2 "$HOME/.claude-tts/daemon-restart.log"
else
    echo "демон не запущен — его поднимет хук SessionStart"
fi
pid="$(player_pid)"
if [ -n "$pid" ]; then
    kill "$pid"
    for _ in $(seq 20); do kill -0 "$pid" 2> /dev/null || break; sleep 0.25; done
    nohup "$BIN_DIR/tts-player" --tray < /dev/null >> "$(state_dir)/player.log" 2>&1 &
    echo "плеер перезапущен"
else
    echo "плеер не запущен — его поднимет хук SessionStart"
fi

if [[ ":$ORIGINAL_PATH:" != *":$BIN_DIR:"* ]]; then
    printf '\nВНИМАНИЕ: %s нет в PATH, а хуки зовут claude-tts оттуда. Выполните `uv tool update-shell`\nи перезапустите терминал и Claude Code.\n' "$BIN_DIR"
fi

printf '\nГотово. Новые сессии Claude Code попадут в плеер сами; уже открытые — после перезапуска.\n'
