"""Окно плеера поверх подменённого контроллера. Открывает окно на экране — только по
TTS_PLAYER_GUI_TESTS=1; трей в тесте не создаётся."""
import os
import unittest

from tts_player.model import Message
from tts_player.player import PlayerState
from tts_player.view import pause_label, play_stop_label, position_label

GUI = os.environ.get("TTS_PLAYER_GUI_TESTS") == "1"

if GUI:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, Gtk  # noqa: E402

    from tts_player.window import PlayerWindow, tray_menu  # noqa: E402


def message(token: str) -> Message:
    return Message("s", token, [], created_at=1_700_000_000.0, project="p", duration_s=10.0)


def state(status: str, current: Message | None, messages: list[Message],
          position_s: float = 0.0) -> PlayerState:
    length = 0.0 if current is None else 10.0
    return PlayerState(status, current, position_s, length, messages, False,
                       [10.0] * len(messages))


class FakePlayer:
    """Контракт Player для окна: state(), on_change и команды кнопок; вызовы записываются."""

    def __init__(self, snapshot: PlayerState) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple] = []
        self.on_change = lambda st: None

    def state(self) -> PlayerState:
        return self.snapshot

    def change(self, snapshot: PlayerState) -> None:
        self.snapshot = snapshot
        self.on_change(snapshot)

    def toggle_pause(self) -> None:
        self.calls.append(("toggle_pause",))

    def toggle_play_stop(self) -> None:
        self.calls.append(("toggle_play_stop",))

    def next(self) -> None:
        self.calls.append(("next",))

    def prev(self) -> None:
        self.calls.append(("prev",))

    def seek(self, position_s: float) -> None:
        self.calls.append(("seek", position_s))

    def clear(self) -> None:
        self.calls.append(("clear",))


def flush() -> None:
    while Gtk.events_pending():
        Gtk.main_iteration()


@unittest.skipUnless(GUI, "GUI-тест открывает окно: запуск с TTS_PLAYER_GUI_TESTS=1")
class PlayerWindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.a, self.b, self.c = message("a"), message("b"), message("c")
        self.player = FakePlayer(state("playing", self.b, [self.a, self.b, self.c], 3.0))
        self.window = PlayerWindow(self.player)
        self.addCleanup(self.window.destroy)

    def test_buttons_call_player_commands(self) -> None:
        w = self.window
        for button, call in ((w.prev_button, "prev"), (w.pause_button, "toggle_pause"),
                             (w.play_stop_button, "toggle_play_stop"),
                             (w.next_button, "next"), (w.clear_button, "clear")):
            self.player.calls.clear()
            button.clicked()
            self.assertEqual([(call,)], self.player.calls, call)

    def test_user_slider_change_seeks(self) -> None:
        self.window.scale.emit("change-value", Gtk.ScrollType.JUMP, 7.0)
        self.assertEqual([("seek", 7.0)], self.player.calls)

    def test_drag_seeks_once_on_release_with_dragged_value(self) -> None:
        w = self.window
        w._on_scale_press(w.scale, None)
        w.scale.emit("change-value", Gtk.ScrollType.JUMP, 4.0)
        w.scale.emit("change-value", Gtk.ScrollType.JUMP, 6.0)
        self.assertEqual([], self.player.calls)
        w._on_scale_release(w.scale, None)
        self.assertEqual([("seek", 6.0)], self.player.calls)

    def test_click_without_movement_does_not_seek(self) -> None:
        w = self.window
        w._on_scale_press(w.scale, None)
        w._on_scale_release(w.scale, None)
        self.assertEqual([], self.player.calls)

    def test_lost_release_does_not_freeze_slider(self) -> None:
        w = self.window
        w._on_scale_press(w.scale, None)
        w.scale.emit("change-value", Gtk.ScrollType.JUMP, 4.0)
        w.scale.emit("grab-broken-event", Gdk.Event.new(Gdk.EventType.GRAB_BROKEN))
        self.player.change(state("playing", self.b, [self.a, self.b, self.c], 8.0))
        self.assertEqual(8.0, w.scale.get_value())
        w._on_scale_release(w.scale, None)
        self.assertEqual([], self.player.calls)

    def test_refresh_timer_only_while_visible(self) -> None:
        self.assertFalse(self.window.refreshing)
        self.window.show_all()
        flush()
        self.assertTrue(self.window.refreshing)
        self.window.hide()
        flush()
        self.assertFalse(self.window.refreshing)

    def test_state_change_updates_widgets_without_seek(self) -> None:
        snapshot = state("paused", self.c, [self.a, self.b, self.c], 5.0)
        self.player.change(snapshot)
        w = self.window
        self.assertEqual([], self.player.calls)
        self.assertEqual(5.0, w.scale.get_value())
        self.assertEqual(position_label(snapshot), w.position.get_text())
        self.assertEqual(pause_label(snapshot), w.pause_button.get_label())
        self.assertEqual(play_stop_label(snapshot), w.play_stop_button.get_label())
        self.assertTrue(w.prev_button.get_sensitive())
        self.assertFalse(w.next_button.get_sensitive())
        self.assertEqual(3, len(w.rows.get_children()))

    def test_stopped_disables_pause(self) -> None:
        self.player.change(state("stopped", self.a, [self.a, self.b, self.c]))
        self.assertFalse(self.window.pause_button.get_sensitive())
        self.assertFalse(self.window.prev_button.get_sensitive())

    def test_close_hides_window(self) -> None:
        self.window.show_all()
        flush()
        self.window.close()  # delete-event, как от кнопки закрытия заголовка
        flush()
        self.assertFalse(self.window.get_visible())
        self.assertIsNotNone(self.window.get_window(), "окно спрятано, а не уничтожено")


@unittest.skipUnless(GUI, "GUI-тест строит меню Gtk: запуск с TTS_PLAYER_GUI_TESTS=1")
class TrayMenuTest(unittest.TestCase):
    def test_quit_item_calls_quit(self) -> None:
        calls = []
        menu = tray_menu(lambda: calls.append("quit"))
        self.addCleanup(menu.destroy)
        items = {item.get_label(): item for item in menu.get_children()}
        self.assertEqual(["Выход"], list(items))
        items["Выход"].activate()
        self.assertEqual(["quit"], calls)


if __name__ == "__main__":
    unittest.main()
