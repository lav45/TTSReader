"""Процесс плеера: разбор аргументов и главный цикл; устройство — в tts_player.player.

Запуск: python3 -m tts_player [--audio-sink NAME] [--history-dir DIR] [--history-db FILE]
                              [--tts-config-dir DIR] [--settings-file FILE]
                              [--tts-python FILE]
                              [--no-gui] [--show] [--tray]
       python3 -m tts_player session add UUID CWD | remove UUID | has UUID
                                  (подкоманда — в tts_player.session_command; has: выход 0 — сессия в списке)
Без --no-gui — Gtk.main, значок в трее и окно (tts_player.window) с экраном «Свой текст»
(tts_player.text_speaker); нет графической сессии — выход 1. С --no-gui — GLib.MainLoop без Gtk
и XApp (e2e).
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

from tts_player.daemon_queue import DaemonQueue  # noqa: E402
from tts_player.daemon_settings import DaemonSettings  # noqa: E402
from tts_player.ducking import Ducker  # noqa: E402
from tts_player.hook_home import HookHome  # noqa: E402
from tts_player.instance import Instance  # noqa: E402
from tts_player.player import Player  # noqa: E402
from tts_player.player_settings import PlayerSettings  # noqa: E402
from tts_player.sessions import Sessions, control_dir, sessions_path  # noqa: E402
from tts_player import session_command  # noqa: E402
from tts_player.text_speaker import TextSpeaker  # noqa: E402

SYNTH_WORKER = Path(__file__).resolve().parent / "synth_worker.py"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["session"]:
        return session_command.main(argv[1:])
    parser = argparse.ArgumentParser(prog="tts_player")
    parser.add_argument("--audio-sink", default=None, help="элемент GStreamer для вывода звука")
    parser.add_argument("--history-dir", default="~/.claude-tts/speech_history",
                        help="каталог склеек демона")
    parser.add_argument("--history-db", default="~/.claude-tts/handy_analysis.db",
                        help="база истории демона (только чтение)")
    parser.add_argument("--tts-config-dir", default="~/.claude-tts",
                        help="каталог claude-tts: голос и скорость (экран настроек,"
                             " переозвучка), очередь демона queue/")
    parser.add_argument("--settings-file",
                        default=str(PlayerSettings.default_path(os.environ)),
                        help="файл настроек плеера: хвост, приглушение")
    parser.add_argument("--tts-python",
                        default="~/.local/share/uv/tools/claude-code-tts/bin/python",
                        help="интерпретатор окружения claude-tts: синтез своего текста")
    parser.add_argument("--no-gui", action="store_true",
                        help="без окна и трея: только плеер в цикле GLib")
    parser.add_argument("--show", action="store_true", help="показать окно сразу при запуске")
    parser.add_argument("--tray", action="store_true",
                        help="запуск в трее со скрытым окном (то же, что без --show)")
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
        shown = "ещё стартует" if pid is None else f"PID {pid}"
        print(f"tts-player уже запущен ({shown})", file=sys.stderr)
        return 0

    if not args.no_gui and not gtk_ready():
        print("tts_player: нет графической сессии, окно и трей открыть негде"
              " (без окна — --no-gui)", file=sys.stderr)
        return 1

    # после блокировки: второй экземпляр не возвращает громкости, приглушённые первым
    ducker = Ducker(Path(runtime) / "tts-player" / "duck.json", os.getpid(),
                    Path(runtime) / "tts-player" / "duck-orphans.json")
    ducker.recover()  # остаток после падения — до первого duck в player.start()
    # подменённый HOME хука: его queue/ плеер забирает, heartbeat держит HookHome
    tts_config_dir = Path(args.tts_config_dir).expanduser()
    hook_home = HookHome(Path(runtime) / "tts-player", tts_config_dir)
    hook_home.start()  # до player.start(): к первой задаче хука hook-home уже готов
    # голос и скорость: экран настроек и переозвучка сообщений с устаревшим кэшем;
    # очередь демона и ссылки hook-home — в том же каталоге claude-tts, чтобы не разошлись
    settings = DaemonSettings(tts_config_dir)
    sessions = Sessions(sessions_path(Path(runtime)))
    sessions.load()  # сессии прошлого запуска — до player.start()
    try:
        player = Player(Path(runtime), args.audio_sink,
                        Path(args.history_dir).expanduser(), Path(args.history_db).expanduser(),
                        ducker, hook_tasks_dir=hook_home.queue_dir,
                        daemon_queue=DaemonQueue(tts_config_dir / "queue"),
                        daemon_settings=settings, sessions=sessions,
                        control_dir=control_dir(Path(runtime)))
        prefs = PlayerSettings(Path(args.settings_file).expanduser())
        player.apply_prefs(prefs.load())  # до start(): накопленное в inbox/ играет по настройкам
        if args.no_gui:
            loop = GLib.MainLoop()
            run(player, loop.run, loop.quit, hook_home=hook_home)
        else:
            text_speaker = TextSpeaker(Path(runtime), Path(args.tts_python).expanduser(),
                                       SYNTH_WORKER, player, args.audio_sink)
            run_gui(player, settings, prefs, text_speaker, args.show, hook_home)
    finally:
        hook_home.stop()  # и когда Player или окно не построились; повторный stop безвреден
    return 0


def gtk_ready() -> bool:
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    initialized, _argv = Gtk.init_check(None)
    return initialized


def run_gui(player: Player, settings: DaemonSettings, prefs: PlayerSettings,
            text_speaker: TextSpeaker, show: bool, hook_home: HookHome) -> None:
    from gi.repository import Gtk

    from tts_player.window import PlayerWindow, TrayIcon

    window = PlayerWindow(player, settings, prefs, text_speaker)
    tray = TrayIcon(window, Gtk.main_quit)  # noqa: F841 — значок живёт, пока жива ссылка
    if show:
        window.present()
    run(player, Gtk.main, Gtk.main_quit, text_speaker, hook_home)


def run(player: Player, main_loop: Callable[[], None], quit_loop: Callable[[], None],
        text_speaker: TextSpeaker | None = None, hook_home: HookHome | None = None) -> None:
    """Плеер в главном цикле; SIGTERM/SIGINT/SIGHUP — выход, сообщения сохраняются в
    messages.json для следующего запуска (Player.shutdown).

    Свой текст (только с окном): остатки прошлых запусков удаляются до player.start(), выход
    TextSpeaker — до player.shutdown(), иначе его release() пришёл бы в остановленный плеер.
    HookHome (запущен до run) останавливается последним, после player.shutdown()."""
    def on_signal() -> bool:
        quit_loop()
        return GLib.SOURCE_REMOVE

    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, on_signal)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, on_signal)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGHUP, on_signal)  # закрытие терминала

    try:
        if text_speaker is not None:
            text_speaker.cleanup_leftovers()
        player.start()
        main_loop()
    finally:
        try:
            try:
                if text_speaker is not None:
                    text_speaker.shutdown()
            finally:
                player.shutdown()
        finally:
            if hook_home is not None:
                hook_home.stop()


if __name__ == "__main__":
    sys.exit(main())
