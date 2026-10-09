"""Окно плеера поверх подменённого контроллера — случаи для бэкенда GTK Broadway.

Запускает tests/test_window_gui.py: поднимает broadwayd и гоняет этот модуль в
подпроцессе с GDK_BACKEND=broadway без DISPLAY/WAYLAND_DISPLAY — на экран пользователя
ничего не попадает. Имя не test_*.py: discover модуль не подбирает. На настоящем
дисплее — только вручную, с TTS_PLAYER_GUI_TESTS=1. Значок XApp в тестах не создаётся.
"""
import os
import unittest

if os.environ.get("GDK_BACKEND") != "broadway" and os.environ.get("TTS_PLAYER_GUI_TESTS") != "1":
    raise unittest.SkipTest("окно открывается только на GDK_BACKEND=broadway"
                            " (tests/test_window_gui.py) или с TTS_PLAYER_GUI_TESTS=1")

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from tts_player.daemon_settings import SettingsError, Voice, VoiceSettings  # noqa: E402
from tts_player.model import Message  # noqa: E402
from tts_player.player import PlayerState  # noqa: E402
from tts_player.view import pause_label, play_stop_label, position_label  # noqa: E402
from tts_player.window import PlayerWindow, tray_menu  # noqa: E402

if not Gtk.init_check(None)[0]:
    raise RuntimeError("Gtk.init_check: дисплей недоступен")


def message(token: str) -> Message:
    return Message("s", token, [], created_at=1_700_000_000.0, project="p", duration_s=10.0)


def state(status: str, current: Message | None, messages: list[Message],
          position_s: float = 0.0) -> PlayerState:
    length = 0.0 if current is None else 10.0
    return PlayerState(status, current, position_s, length, messages, False,
                       [10.0] * len(messages), [True] * len(messages))


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


class FakeSettings:
    """Контракт DaemonSettings для окна: load() и save(); вызовы записываются.

    load_error / save_error — SettingsError, которую бросит соответствующий вызов."""

    def __init__(self, loaded: VoiceSettings) -> None:
        self.loaded = loaded
        self.load_error: SettingsError | None = None
        self.save_error: SettingsError | None = None
        self.calls: list[tuple] = []

    def load(self) -> VoiceSettings:
        self.calls.append(("load",))
        if self.load_error is not None:
            raise self.load_error
        return self.loaded

    def save(self, persona: str, speed: float) -> None:
        self.calls.append(("save", persona, speed))
        if self.save_error is not None:
            raise self.save_error


def voice_settings() -> VoiceSettings:
    return VoiceSettings(voices=[Voice("aria", "Тёплый женский"), Voice("bob", "")],
                         active="bob", speed=1.5)


def icon_name(button: Gtk.Button) -> str:
    name, _size = button.get_image().get_icon_name()
    return name


def flush() -> None:
    while Gtk.events_pending():
        Gtk.main_iteration()


class PlayerWindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.a, self.b, self.c = message("a"), message("b"), message("c")
        self.player = FakePlayer(state("playing", self.b, [self.a, self.b, self.c], 3.0))
        self.settings = FakeSettings(voice_settings())
        self.window = PlayerWindow(self.player, self.settings)
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
        self.assertEqual(position_label(snapshot), w.position_label_widget.get_text())
        self.assertEqual(pause_label(snapshot), w.pause_button.get_label())
        self.assertEqual(play_stop_label(snapshot), w.play_stop_button.get_label())
        self.assertTrue(w.prev_button.get_sensitive())
        self.assertFalse(w.next_button.get_sensitive())
        self.assertEqual(3, len(w.message_list.get_children()))

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


class SettingsScreenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.player = FakePlayer(state("stopped", None, []))
        self.settings = FakeSettings(voice_settings())
        self.window = PlayerWindow(self.player, self.settings)
        self.addCleanup(self.window.destroy)

    def test_icon_buttons_without_text_first_in_one_row(self) -> None:
        w = self.window
        for button, icon, tooltip in ((w.clear_button, "user-trash-symbolic", "Очистить буфер"),
                                      (w.settings_button, "emblem-system-symbolic", "Настройки")):
            self.assertFalse(button.get_label(), tooltip)
            self.assertEqual(icon, icon_name(button))
            self.assertEqual(tooltip, button.get_tooltip_text())
        row = w.clear_button.get_parent()
        self.assertIs(row, w.settings_button.get_parent())
        self.assertEqual(Gtk.Orientation.HORIZONTAL, row.get_orientation())
        self.assertEqual([w.clear_button, w.settings_button], row.get_children()[:2])
        for button in (w.clear_button, w.settings_button):
            self.assertEqual(Gtk.PackType.START,
                             row.child_get_property(button, "pack-type"))

    def test_main_screen_at_start(self) -> None:
        self.assertEqual("main", self.window.stack.get_visible_child_name())
        self.assertEqual([], self.settings.calls)

    def test_settings_button_opens_settings_with_loaded_values(self) -> None:
        w = self.window
        w.settings_button.clicked()
        self.assertEqual("settings", w.stack.get_visible_child_name())
        self.assertEqual([("load",)], self.settings.calls)
        model = w.voice_combo.get_model()
        self.assertEqual(["aria — Тёплый женский", "bob"], [row[0] for row in model])
        self.assertEqual("bob", w.voice_combo.get_active_id())
        self.assertEqual(1.5, w.speed_spin.get_value())
        for widget in (w.voice_combo, w.speed_spin, w.save_button):
            self.assertTrue(widget.get_sensitive())
        self.assertEqual("Сохранить", w.save_button.get_label())
        self.assertEqual(icon_name(w.back_button), "go-previous-symbolic")

    def test_speed_spin_range(self) -> None:
        spin = self.window.speed_spin
        self.assertEqual((0.5, 4.0), tuple(spin.get_range()))
        self.assertEqual(0.1, round(spin.get_increments()[0], 6))
        self.assertEqual(1, spin.get_digits())

    def test_reopen_reloads_without_duplicating_voices(self) -> None:
        w = self.window
        w.settings_button.clicked()
        w.back_button.clicked()
        w.settings_button.clicked()
        self.assertEqual(2, len(w.voice_combo.get_model()))
        self.assertEqual([("load",), ("load",)], self.settings.calls)

    def test_load_error_disables_controls_and_shows_reason(self) -> None:
        w = self.window
        self.settings.load_error = SettingsError("config.json не прочитан")
        w.settings_button.clicked()
        self.assertEqual("settings", w.stack.get_visible_child_name())
        for widget in (w.voice_combo, w.speed_spin, w.save_button):
            self.assertFalse(widget.get_sensitive())
        self.assertEqual("config.json не прочитан", w.settings_error.get_text())
        w.back_button.clicked()
        self.assertEqual("main", w.stack.get_visible_child_name())

    def test_successful_load_after_error_clears_reason(self) -> None:
        w = self.window
        self.settings.load_error = SettingsError("сбой")
        w.settings_button.clicked()
        w.back_button.clicked()
        self.settings.load_error = None
        w.settings_button.clicked()
        self.assertEqual("", w.settings_error.get_text())
        self.assertTrue(w.save_button.get_sensitive())

    def test_save_sends_chosen_voice_and_speed_and_returns(self) -> None:
        w = self.window
        w.settings_button.clicked()
        w.voice_combo.set_active_id("aria")
        w.speed_spin.set_value(2.3)
        w.save_button.clicked()
        self.assertEqual(("save", "aria", 2.3), self.settings.calls[-1])
        self.assertEqual("main", w.stack.get_visible_child_name())

    def test_save_error_keeps_screen_and_shows_reason(self) -> None:
        w = self.window
        w.settings_button.clicked()
        self.settings.save_error = SettingsError("не удалось записать")
        w.save_button.clicked()
        self.assertEqual("settings", w.stack.get_visible_child_name())
        self.assertEqual("не удалось записать", w.settings_error.get_text())

    def test_back_returns_without_save(self) -> None:
        w = self.window
        w.settings_button.clicked()
        w.voice_combo.set_active_id("aria")
        w.back_button.clicked()
        self.assertEqual("main", w.stack.get_visible_child_name())
        self.assertEqual([("load",)], self.settings.calls)


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
