"""Процесс плеера: разбор аргументов и главный цикл; устройство — в tts_player.player.

Запуск: python3 -m tts_player [--audio-sink NAME] [--history-dir DIR] [--history-db FILE]
                              [--no-gui] [--show] [--tray]
       python3 -m tts_player stop    (SIGUSR1 запущенному плееру: остановить текущее)
Без --no-gui — Gtk.main, значок в трее и окно (tts_player.window); нет графической сессии —
выход 1. С --no-gui — GLib.MainLoop без Gtk и XApp (e2e).
"""
import argparse
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Callable

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from tts_player.instance import Instance, send_stop  # noqa: E402
from tts_player.player import Player  # noqa: E402

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["stop"]:
        return stop_command()
    parser = argparse.ArgumentParser(prog="tts_player")
    parser.add_argument("--audio-sink", default=None, help="элемент GStreamer для вывода звука")
    parser.add_argument("--history-dir", default="~/.claude-tts/speech_history",
                        help="каталог склеек демона")
    parser.add_argument("--history-db", default="~/.claude-tts/handy_analysis.db",
                        help="база истории демона (только чтение)")
    parser.add_argument("--no-gui", action="store_true",
                        help="без окна и трея: только плеер в цикле GLib")
    parser.add_argument("--show", action="store_true", help="показать окно сразу при запуске")
    parser.add_argument("--tray", action="store_true",
                        help="запуск в трее с скрытым окном (то же, что без --show)")
    args = parser.parse_args(argv)
    if args.tray and (args.show or args.no_gui):
        parser.error("--tray не сочетается с --show и --no-gui")
    if args.no_gui and args.show:
        parser.error("--show не сочетается с --no-gui")
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        print("tts_player: XDG_RUNTIME_DIR не задан", file=sys.stderr)
        return 1

    instance = Instance.acquire(Path(runtime))  # до Player(): второй экземпляр store/ не трогает
    if instance is None:
        pid = Instance.holder_pid(Path(runtime))
        print(f"tts-player уже запущен (PID {pid})", file=sys.stderr)
        return 0

    if not args.no_gui and not gtk_ready():
        print("tts_player: нет графической сессии, окно и трей открыть негде"
              " (без окна — --no-gui)", file=sys.stderr)
        return 1

    player = Player(Path(runtime), args.audio_sink,
                    Path(args.history_dir).expanduser(), Path(args.history_db).expanduser())
    if args.no_gui:
        loop = GLib.MainLoop()
        run(player, loop.run, loop.quit)
    else:
        run_gui(player, args.show)
    return 0


def stop_command() -> int:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        print("tts_player: XDG_RUNTIME_DIR не задан", file=sys.stderr)
        return 1
    send_stop(Path(runtime))
    return 0


def gtk_ready() -> bool:
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    initialized, _argv = Gtk.init_check(None)
    return initialized


def run_gui(player: Player, show: bool) -> None:
    from gi.repository import Gtk

    from tts_player.window import PlayerWindow, TrayIcon

    window = PlayerWindow(player)
    tray = TrayIcon(window, Gtk.main_quit)  # noqa: F841 — значок живёт, пока жива ссылка
    if show:
        window.present()
    run(player, Gtk.main, Gtk.main_quit)


def run(player: Player, main_loop: Callable[[], None], quit_loop: Callable[[], None]) -> None:
    """Плеер в главном цикле; SIGTERM/SIGINT — выход, незаконченное — обратно в inbox/."""
    def on_signal() -> bool:
        quit_loop()
        return GLib.SOURCE_REMOVE

    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, on_signal)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, on_signal)

    def on_stop() -> bool:
        log.info("stop requested")
        player.stop_playback()
        return GLib.SOURCE_CONTINUE

    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGUSR1, on_stop)
    player.start()
    try:
        main_loop()
    finally:
        player.shutdown()


if __name__ == "__main__":
    sys.exit(main())
