"""Окно плеера поверх подменённого контроллера — случаи для бэкенда GTK Broadway.

Запускает tests/test_window_gui.py: поднимает broadwayd и гоняет этот модуль в
подпроцессе с GDK_BACKEND=broadway без DISPLAY/WAYLAND_DISPLAY — на экран пользователя
ничего не попадает. Имя не test_*.py: discover модуль не подбирает. На настоящем
дисплее — только вручную, с TTS_PLAYER_GUI_TESTS=1. Значок XApp в тестах не создаётся.
"""
import locale
import os
import unittest

if os.environ.get("GDK_BACKEND") != "broadway" and os.environ.get("TTS_PLAYER_GUI_TESTS") != "1":
    raise unittest.SkipTest("окно открывается только на GDK_BACKEND=broadway"
                            " (tests/test_window_gui.py) или с TTS_PLAYER_GUI_TESTS=1")

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from tts_player.daemon_settings import Voice, VoiceSettings  # noqa: E402
from tts_player.settings_error import SettingsError  # noqa: E402
from tts_player.text_speaker import TextState  # noqa: E402
from tts_player.model import Message  # noqa: E402
from tts_player.player import PlayerState  # noqa: E402
from tts_player.player_settings import Prefs  # noqa: E402
from tts_player.sessions import Session  # noqa: E402
from tts_player.view import play_pause_label, position_label  # noqa: E402
from tts_player.window import PlayerWindow, tray_menu  # noqa: E402

if not Gtk.init_check(None)[0]:
    raise RuntimeError("Gtk.init_check: дисплей недоступен")


SESSIONS = [Session("1dd2b064-b8fd-4c1e-9a77-000000000001", "TTSReader"),
            Session("abcdef12-0000-4000-8000-000000000002", "")]


def message(task: str) -> Message:
    return Message("s", task_ids={task}, created_at=1_700_000_000.0, project="p", duration_s=10.0)


def state(status: str, current: Message | None, messages: list[Message],
          position_s: float = 0.0, autoplay: bool = True, sessions: tuple = (),
          active: str | None = None) -> PlayerState:
    length = 0.0 if current is None else 10.0
    return PlayerState(status, current, position_s, length, messages, False,
                       [10.0] * len(messages), [True] * len(messages), autoplay,
                       list(sessions), active)


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

    def toggle_play_pause(self) -> None:
        self.calls.append(("toggle_play_pause",))

    def apply_prefs(self, prefs: Prefs) -> None:
        """Как Player: настройки применены, окно получает новый снимок с autoplay."""
        self.calls.append(("apply_prefs", prefs))
        snapshot = self.snapshot
        self.change(PlayerState(snapshot.status, snapshot.current, snapshot.position_s,
                                snapshot.length_s, snapshot.messages, snapshot.output_down,
                                snapshot.lengths, snapshot.completes, prefs.autoplay,
                                snapshot.sessions, snapshot.active))

    def next(self) -> None:
        self.calls.append(("next",))

    def prev(self) -> None:
        self.calls.append(("prev",))

    def seek(self, position_s: float) -> None:
        self.calls.append(("seek", position_s))

    def clear(self) -> None:
        self.calls.append(("clear",))

    def play_message(self, message: Message) -> None:
        self.calls.append(("play_message", message))

    def select_session(self, session_id: str) -> None:
        self.calls.append(("select_session", session_id))

    def remove_session(self, session_id: str) -> None:
        self.calls.append(("remove_session", session_id))


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


class FakePrefs:
    """Контракт PlayerSettings для окна: load() и save(); вызовы записываются.

    save_error — SettingsError, которую бросит save(); сохранённое становится загружаемым."""

    def __init__(self, loaded: Prefs) -> None:
        self.loaded = loaded
        self.save_error: SettingsError | None = None
        self.calls: list[tuple] = []

    def load(self) -> Prefs:
        self.calls.append(("load",))
        return self.loaded

    def save(self, prefs: Prefs) -> None:
        self.calls.append(("save", prefs))
        if self.save_error is not None:
            raise self.save_error
        self.loaded = prefs


def text_state(status: str, position_s: float = 0.0, length_s: float = 0.0,
               error: str = "") -> TextState:
    return TextState(status, position_s, length_s, error)


class FakeTextSpeaker:
    """Контракт TextSpeaker для окна: state(), on_change и команды экрана «Свой текст»;
    вызовы записываются. toggle_play_pause из playing и synthesizing ставит paused, как
    настоящий, — окно видит паузу своего текста раньше команды главному плееру."""

    def __init__(self, snapshot: TextState | None = None) -> None:
        self.snapshot = snapshot or text_state("empty")
        self.calls: list[tuple] = []
        self.on_change = lambda st: None

    def state(self) -> TextState:
        return self.snapshot

    def change(self, snapshot: TextState) -> None:
        self.snapshot = snapshot
        self.on_change(snapshot)

    def set_text(self, text: str) -> None:
        self.calls.append(("set_text", text))

    def toggle_play_pause(self) -> None:
        self.calls.append(("toggle_play_pause",))
        if self.snapshot.status in ("playing", "synthesizing"):
            self.change(text_state("paused", self.snapshot.position_s, self.snapshot.length_s))

    def seek(self, position_s: float) -> None:
        self.calls.append(("seek", position_s))


def voice_settings() -> VoiceSettings:
    return VoiceSettings(voices=[Voice("aria", "Тёплый женский"), Voice("bob", "")],
                         active="bob", speed=1.5)


def css_classes(widget: Gtk.Widget) -> set[str]:
    context = widget.get_style_context()
    return {name for name in ("success", "error") if context.has_class(name)}


def icon_name(button: Gtk.Button) -> str:
    name, _size = button.get_image().get_icon_name()
    return name


def flush() -> None:
    while Gtk.events_pending():
        Gtk.main_iteration()


class PlayerWindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.a, self.b, self.c = message("a"), message("b"), message("c")
        self.player = FakePlayer(state("playing", self.b, [self.a, self.b, self.c], 3.0,
                                       sessions=SESSIONS, active=SESSIONS[0].id))
        self.settings = FakeSettings(voice_settings())
        self.prefs = FakePrefs(Prefs(tail=7, autoplay=True))
        self.text = FakeTextSpeaker()
        self.window = PlayerWindow(self.player, self.settings, self.prefs,
                                   self.text)
        self.addCleanup(self.window.destroy)

    def test_buttons_call_player_commands(self) -> None:
        w = self.window
        for button, call in ((w.prev_button, "prev"), (w.play_pause_button, "toggle_play_pause"),
                             (w.next_button, "next"), (w.clear_button, "clear")):
            self.player.calls.clear()
            button.clicked()
            self.assertEqual([(call,)], self.player.calls, call)

    def test_end_session_button_is_third_icon_without_text(self) -> None:
        w = self.window
        button = w.end_session_button
        self.assertFalse(button.get_label())
        self.assertEqual("system-shutdown-symbolic", icon_name(button))
        self.assertEqual("Завершить активную сессию", button.get_tooltip_text())
        self.assertEqual("Очистить сообщения активной сессии", w.clear_button.get_tooltip_text())
        row = w.clear_button.get_parent()
        self.assertEqual([w.settings_button, w.clear_button, button], row.get_children()[:3])

    def test_end_session_button_removes_active_session_once(self) -> None:
        self.player.calls.clear()
        self.window.end_session_button.clicked()
        self.assertEqual([("remove_session", SESSIONS[0].id)], self.player.calls)

    def test_session_buttons_follow_active_session(self) -> None:
        w = self.window
        self.assertTrue(w.clear_button.get_sensitive())
        self.assertTrue(w.end_session_button.get_sensitive())
        self.player.change(state("playing", self.b, [self.a, self.b, self.c], 3.0,
                                 sessions=SESSIONS))
        self.assertFalse(w.clear_button.get_sensitive())
        self.assertFalse(w.end_session_button.get_sensitive())

    def test_session_list_sits_between_tools_and_messages(self) -> None:
        w = self.window
        main = w.clear_button.get_parent().get_parent()
        children = main.get_children()
        tools = w.clear_button.get_parent()
        container = w.session_list.get_ancestor(Gtk.Box)
        while container.get_parent() is not main:
            container = container.get_parent()
        scrolled = w.message_list.get_ancestor(Gtk.ScrolledWindow)
        self.assertEqual(children.index(tools) + 1, children.index(container))
        self.assertEqual(children.index(container) + 1, children.index(scrolled))
        self.assertFalse(main.query_child_packing(container)[0])  # expand не отнимает

    def session_texts(self) -> list[str]:
        rows = self.window.session_list.get_children()
        return [row.get_child().get_children()[0].get_text() for row in rows]

    def test_session_list_shows_rows_with_bold_active(self) -> None:
        self.assertEqual(["TTSReader · 1dd2b064", "— · abcdef12"], self.session_texts())
        labels = [row.get_child().get_children()[0]
                  for row in self.window.session_list.get_children()]
        bold = [label.get_attributes() is not None for label in labels]
        self.assertEqual([True, False], bold)

    def test_session_row_activation_selects_that_session(self) -> None:
        w = self.window
        self.assertTrue(w.session_list.get_activate_on_single_click())
        self.player.calls.clear()
        w.session_list.get_row_at_index(1).activate()
        w.session_list.get_row_at_index(0).activate()
        self.assertEqual([("select_session", SESSIONS[1].id), ("select_session", SESSIONS[0].id)],
                         self.player.calls)

    def focused_session(self) -> int | None:
        row = self.window.session_list.get_focus_child()
        return None if row is None else row.get_index()

    def test_session_rebuild_keeps_focus_on_active_row(self) -> None:
        w = self.window
        w.show_all()
        flush()
        w.session_list.get_row_at_index(0).grab_focus()
        flush()
        self.assertEqual(0, self.focused_session())
        # Enter по второй строке: активной стала она, строки перестроились
        self.player.change(state("playing", self.b, [self.a, self.b, self.c], 3.0,
                                 sessions=SESSIONS, active=SESSIONS[1].id))
        flush()
        self.assertEqual(1, self.focused_session())

    def test_session_rebuild_without_active_keeps_same_index(self) -> None:
        w = self.window
        w.show_all()
        flush()
        w.session_list.get_row_at_index(1).grab_focus()
        flush()
        self.player.change(state("playing", self.b, [self.a, self.b, self.c], 3.0,
                                 sessions=SESSIONS[:1]))
        flush()
        self.assertEqual(0, self.focused_session())

    def test_no_sessions_hides_list_and_disables_buttons(self) -> None:
        w = self.window
        container = w.session_list.get_parent()
        self.assertTrue(container.get_visible())
        self.player.change(state("playing", self.b, [self.a, self.b, self.c], 3.0))
        self.assertEqual([], w.session_list.get_children())
        self.assertFalse(container.get_visible())
        self.assertFalse(w.clear_button.get_sensitive())
        self.assertFalse(w.end_session_button.get_sensitive())
        self.player.change(state("playing", self.b, [self.a, self.b, self.c], 3.0,
                                 sessions=SESSIONS, active=SESSIONS[0].id))
        self.assertTrue(container.get_visible())

    def test_session_list_rebuilds_only_on_change(self) -> None:
        w = self.window
        row = w.session_list.get_row_at_index(0)
        self.player.change(state("playing", self.b, [self.a, self.b, self.c], 4.0,
                                 sessions=SESSIONS, active=SESSIONS[0].id))
        self.assertIs(row, w.session_list.get_row_at_index(0))
        self.player.change(state("playing", self.b, [self.a, self.b, self.c], 4.0,
                                 sessions=SESSIONS, active=SESSIONS[1].id))
        self.assertIsNot(row, w.session_list.get_row_at_index(0))

    def test_row_activation_selects_that_message(self) -> None:
        w = self.window
        self.assertTrue(w.message_list.get_activate_on_single_click())
        w.message_list.get_row_at_index(1).activate()
        self.assertEqual(1, len(self.player.calls))
        name, played = self.player.calls[0]
        self.assertEqual("play_message", name)
        self.assertIs(self.b, played)

    def test_row_activation_after_rebuild_uses_new_snapshot(self) -> None:
        w = self.window
        self.player.change(state("playing", self.b, [self.c, self.a, self.b], 3.0))
        w.message_list.emit("row-activated", w.message_list.get_row_at_index(0))
        self.assertEqual(1, len(self.player.calls))
        self.assertIs(self.c, self.player.calls[0][1])

    def test_row_activation_with_equal_rows_uses_latest_message_objects(self) -> None:
        w = self.window
        a2, b2, c2 = message("a"), message("b"), message("c")
        self.player.change(state("playing", b2, [a2, b2, c2], 4.0))
        row = w.message_list.get_row_at_index(2)
        self.player.change(state("playing", b2, [a2, b2, c2], 4.0))
        self.assertIs(row, w.message_list.get_row_at_index(2))  # строки равны: без перестройки
        w.message_list.get_row_at_index(2).activate()
        self.assertIs(c2, self.player.calls[0][1])

    def focused_index(self) -> int | None:
        row = self.window.message_list.get_focus_child()
        return None if row is None else row.get_index()

    def test_rebuild_keeps_keyboard_focus_on_current_row(self) -> None:
        w = self.window
        w.show_all()
        flush()
        w.message_list.get_row_at_index(0).grab_focus()
        flush()
        self.assertEqual(0, self.focused_index())
        # клик по строке: текущим стало сообщение c, список перестроился
        self.player.change(state("playing", self.c, [self.a, self.b, self.c], 0.0))
        flush()
        self.assertEqual(2, self.focused_index())

    def test_rebuild_without_current_keeps_focus_on_same_index(self) -> None:
        w = self.window
        w.show_all()
        flush()
        w.message_list.get_row_at_index(1).grab_focus()
        flush()
        self.player.change(state("stopped", None, [self.a, self.b], 0.0))
        flush()
        self.assertEqual(1, self.focused_index())

    def test_rebuild_does_not_steal_focus_from_elsewhere(self) -> None:
        w = self.window
        w.show_all()
        flush()
        w.next_button.grab_focus()
        flush()
        self.player.change(state("playing", self.c, [self.a, self.b, self.c], 0.0))
        flush()
        self.assertIsNone(self.focused_index())

    def test_user_slider_change_seeks(self) -> None:
        self.window.scale.emit("change-value", Gtk.ScrollType.JUMP, 7.0)
        self.assertEqual([("seek", 7.0)], self.player.calls)

    def test_drag_seeks_once_on_release_with_dragged_value(self) -> None:
        w = self.window
        w.slider.press()
        w.scale.emit("change-value", Gtk.ScrollType.JUMP, 4.0)
        w.scale.emit("change-value", Gtk.ScrollType.JUMP, 6.0)
        self.assertEqual([], self.player.calls)
        w.slider.release()
        self.assertEqual([("seek", 6.0)], self.player.calls)

    def test_click_without_movement_does_not_seek(self) -> None:
        w = self.window
        w.slider.press()
        w.slider.release()
        self.assertEqual([], self.player.calls)

    def test_lost_release_does_not_freeze_slider(self) -> None:
        w = self.window
        w.slider.press()
        w.scale.emit("change-value", Gtk.ScrollType.JUMP, 4.0)
        w.scale.emit("grab-broken-event", Gdk.Event.new(Gdk.EventType.GRAB_BROKEN))
        self.player.change(state("playing", self.b, [self.a, self.b, self.c], 8.0))
        self.assertEqual(8.0, w.scale.get_value())
        w.slider.release()
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
        self.assertEqual(play_pause_label(snapshot), w.play_pause_button.get_label())
        self.assertTrue(w.prev_button.get_sensitive())
        self.assertFalse(w.next_button.get_sensitive())
        self.assertEqual(3, len(w.message_list.get_children()))

    def test_transport_row_is_position_left_then_prev_play_pause_next_right(self) -> None:
        w = self.window
        row = w.play_pause_button.get_parent()
        buttons = [child for child in row.get_children() if isinstance(child, Gtk.Button)]
        self.assertEqual([w.prev_button, w.play_pause_button, w.next_button], buttons)
        self.assertIs(row, w.position_label_widget.get_parent())
        self.assertEqual(Gtk.PackType.START,
                         row.child_get_property(w.position_label_widget, "pack-type"))
        self.assertEqual(w.position_label_widget, row.get_children()[0])
        for button in buttons:
            self.assertEqual(Gtk.PackType.END, row.child_get_property(button, "pack-type"))
        # pack_end кладёт последним добавленного первым справа: порядок слева направо
        self.assertEqual([w.position_label_widget, w.prev_button, w.play_pause_button,
                          w.next_button], row.get_children())
        for name in ("pause_button", "play_stop_button"):
            self.assertFalse(hasattr(w, name), name)

    def test_main_screen_order_top_to_bottom(self) -> None:
        w = self.window
        main = w.stack.get_child_by_name("main")
        tools = w.clear_button.get_parent()
        scrolled = w.message_list.get_parent()
        while scrolled.get_parent() is not main:  # ListBox в ScrolledWindow через Viewport
            scrolled = scrolled.get_parent()
        transport = w.play_pause_button.get_parent()
        sessions = w.session_list.get_parent()  # свой контейнер между значками и сообщениями
        self.assertEqual([tools, sessions, scrolled, w.scale, transport], main.get_children())
        self.assertTrue(main.child_get_property(scrolled, "expand"))

    def test_play_pause_label_and_sensitivity_per_state(self) -> None:
        w = self.window
        heard = message("h")
        heard.heard = True
        cases = (
            (state("playing", self.a, [self.a]), "⏸", True),
            (state("paused", self.a, [self.a]), "⏵", True),
            (state("stopped", self.a, [self.a]), "⏵", True),
            (state("idle", None, []), "⏵", False),
            (state("idle", None, [heard, self.b]), "⏵", True),
        )
        for snapshot, label, enabled in cases:
            self.player.change(snapshot)
            self.assertEqual(label, w.play_pause_button.get_label(), snapshot.status)
            self.assertEqual(enabled, w.play_pause_button.get_sensitive(), snapshot.status)

    def test_stopped_first_disables_prev(self) -> None:
        self.player.change(state("stopped", self.a, [self.a, self.b, self.c]))
        self.assertFalse(self.window.prev_button.get_sensitive())

    def test_autoplay_button_icon_tooltip_right_in_tools_row(self) -> None:
        w = self.window
        button = w.autoplay_button
        self.assertIsInstance(button, Gtk.ToggleButton)
        self.assertFalse(button.get_label())
        self.assertEqual("media-playlist-consecutive-symbolic", icon_name(button))
        self.assertEqual("Автовоспроизведение", button.get_tooltip_text())
        row = w.clear_button.get_parent()
        self.assertIs(row, button.get_parent())
        self.assertEqual(Gtk.PackType.END, row.child_get_property(button, "pack-type"))
        self.assertTrue(button.get_active())

    def test_autoplay_toggle_saves_prefs_and_applies(self) -> None:
        """Остальные ключи (хвост, приглушение) — из файла, меняется только autoplay."""
        w = self.window
        self.prefs.loaded = Prefs(tail=7, autoplay=True, duck=45)
        w.autoplay_button.set_active(False)
        expected = Prefs(tail=7, autoplay=False, duck=45)
        self.assertIn(("save", expected), self.prefs.calls)
        self.assertEqual([("apply_prefs", expected)], self.player.calls)
        self.assertFalse(w.autoplay_button.get_active())

    def test_autoplay_follows_state_without_save(self) -> None:
        w = self.window
        self.player.change(state("playing", self.b, [self.a, self.b, self.c], autoplay=False))
        self.assertFalse(w.autoplay_button.get_active())
        self.player.change(state("playing", self.b, [self.a, self.b, self.c], autoplay=True))
        self.assertTrue(w.autoplay_button.get_active())
        self.assertEqual([], [call for call in self.prefs.calls if call[0] == "save"])
        self.assertEqual([], self.player.calls)

    def test_autoplay_save_error_reverts_button_and_logs(self) -> None:
        w = self.window
        self.prefs.save_error = SettingsError("не удалось записать settings.json")
        with self.assertLogs("tts_player", "WARNING") as logs:
            w.autoplay_button.set_active(False)
        self.assertIn("не удалось записать settings.json", logs.output[0])
        self.assertTrue(w.autoplay_button.get_active())
        self.assertEqual([], self.player.calls)
        saves = [call for call in self.prefs.calls if call[0] == "save"]
        self.assertEqual(1, len(saves), "возврат кнопки не сохраняет повторно")

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
        self.prefs = FakePrefs(Prefs(tail=7, autoplay=True))
        self.text = FakeTextSpeaker()
        self.window = PlayerWindow(self.player, self.settings, self.prefs,
                                   self.text)
        self.addCleanup(self.window.destroy)

    def test_icon_buttons_without_text_first_in_one_row(self) -> None:
        w = self.window
        clear_tooltip = "Очистить сообщения активной сессии"
        for button, icon, tooltip in ((w.settings_button, "emblem-system-symbolic", "Настройки"),
                                      (w.clear_button, "user-trash-symbolic", clear_tooltip)):
            self.assertFalse(button.get_label(), tooltip)
            self.assertEqual(icon, icon_name(button))
            self.assertEqual(tooltip, button.get_tooltip_text())
        row = w.clear_button.get_parent()
        self.assertIs(row, w.settings_button.get_parent())
        self.assertEqual(Gtk.Orientation.HORIZONTAL, row.get_orientation())
        self.assertEqual([w.settings_button, w.clear_button], row.get_children()[:2])
        for button in (w.settings_button, w.clear_button):
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
        for widget in (w.voice_combo, w.speed_spin, w.tail_spin, w.duck_spin, w.save_button):
            self.assertTrue(widget.get_sensitive())
        self.assertEqual(7, w.tail_spin.get_value_as_int())
        self.assertEqual(30, w.duck_spin.get_value_as_int())
        self.assertFalse(w.settings_status.get_visible())
        self.assertEqual("Сохранить", w.save_button.get_label())
        self.assertEqual(icon_name(w.back_button), "go-previous-symbolic")

    def test_speed_spin_range(self) -> None:
        spin = self.window.speed_spin
        self.assertEqual((0.5, 4.0), tuple(spin.get_range()))
        self.assertEqual(0.1, round(spin.get_increments()[0], 6))
        self.assertEqual(2, spin.get_digits())

    def test_save_keeps_two_decimal_speed_untouched(self) -> None:
        w = self.window
        self.settings.loaded = VoiceSettings(voices=[Voice("bob", "")], active="bob", speed=1.25)
        w.settings_button.clicked()
        w.speed_spin.update()  # то же, что потеря фокуса: значение заново из текста
        w.save_button.clicked()
        self.assertEqual(("save", "bob", 1.25), self.settings.calls[-1])

    def test_save_rounds_spin_arrow_float_noise(self) -> None:
        w = self.window
        self.settings.loaded = VoiceSettings(voices=[Voice("bob", "")], active="bob", speed=1.8)
        w.settings_button.clicked()
        w.speed_spin.spin(Gtk.SpinType.STEP_FORWARD, 0.1)
        w.save_button.clicked()
        self.assertEqual(("save", "bob", 1.9), self.settings.calls[-1])

    def test_save_takes_typed_text_without_focus_loss(self) -> None:
        w = self.window
        w.settings_button.clicked()
        w.speed_spin.set_text(locale.format_string("%.1f", 2.7))  # разделитель — по локали
        w.save_button.clicked()
        self.assertEqual(("save", "bob", 2.7), self.settings.calls[-1])

    def test_long_voice_description_does_not_widen_window(self) -> None:
        w = self.window
        self.settings.loaded = VoiceSettings(
            voices=[Voice("bob", "очень длинное описание " * 10)], active="bob", speed=1.5)
        w.settings_button.clicked()
        w.show_all()
        flush()
        minimum, _natural = w.get_preferred_width()
        self.assertLessEqual(minimum, 420)

    def test_reopen_reloads_without_duplicating_voices(self) -> None:
        w = self.window
        w.settings_button.clicked()
        w.back_button.clicked()
        w.settings_button.clicked()
        self.assertEqual(2, len(w.voice_combo.get_model()))
        self.assertEqual([("load",), ("load",)], self.settings.calls)

    def test_tail_spin_range(self) -> None:
        spin = self.window.tail_spin
        self.assertEqual((1.0, 20.0), tuple(spin.get_range()))
        self.assertEqual(1.0, spin.get_increments()[0])
        self.assertEqual(0, spin.get_digits())

    def test_tail_spin_reloaded_on_each_open(self) -> None:
        w = self.window
        w.settings_button.clicked()
        w.tail_spin.set_value(3)
        w.back_button.clicked()
        self.prefs.loaded = Prefs(tail=12, autoplay=True)
        w.settings_button.clicked()
        self.assertEqual(12, w.tail_spin.get_value_as_int())

    def test_load_error_disables_voice_controls_and_shows_reason(self) -> None:
        w = self.window
        self.settings.load_error = SettingsError("config.json не прочитан")
        w.settings_button.clicked()
        self.assertEqual("settings", w.stack.get_visible_child_name())
        for widget in (w.voice_combo, w.speed_spin):
            self.assertFalse(widget.get_sensitive())
        for widget in (w.tail_spin, w.duck_spin, w.save_button):
            self.assertTrue(widget.get_sensitive())
        self.assertTrue(w.settings_status.get_visible())
        self.assertEqual("config.json не прочитан", w.settings_status.get_text())
        self.assertEqual({"error"}, css_classes(w.settings_status))
        w.back_button.clicked()
        self.assertEqual("main", w.stack.get_visible_child_name())

    def test_load_error_save_writes_only_player_prefs(self) -> None:
        w = self.window
        self.settings.load_error = SettingsError("config.json не прочитан")
        w.settings_button.clicked()
        w.tail_spin.set_value(9)
        w.duck_spin.set_value(55)
        w.save_button.clicked()
        expected = Prefs(tail=9, autoplay=True, duck=55)
        self.assertEqual(("save", expected), self.prefs.calls[-1])
        self.assertEqual([("apply_prefs", expected)], self.player.calls)
        self.assertEqual([("load",)], self.settings.calls)
        self.assertEqual({"success"}, css_classes(w.settings_status))

    def test_successful_load_after_error_clears_reason(self) -> None:
        w = self.window
        self.settings.load_error = SettingsError("сбой")
        w.settings_button.clicked()
        w.back_button.clicked()
        self.settings.load_error = None
        w.settings_button.clicked()
        self.assertFalse(w.settings_status.get_visible())
        self.assertEqual("", w.settings_status.get_text())
        self.assertEqual(set(), css_classes(w.settings_status))
        self.assertTrue(w.speed_spin.get_sensitive())

    def test_save_writes_prefs_then_voice_and_stays_with_success(self) -> None:
        w = self.window
        self.player.change(state("stopped", None, [], autoplay=False))
        w.settings_button.clicked()
        w.voice_combo.set_active_id("aria")
        w.speed_spin.set_value(2.3)
        w.tail_spin.set_value(4)
        w.duck_spin.set_value(60)
        w.save_button.clicked()
        expected = Prefs(tail=4, autoplay=False, duck=60)
        self.assertEqual(("save", expected), self.prefs.calls[-1])
        self.assertEqual([("apply_prefs", expected)], self.player.calls)
        self.assertEqual(("save", "aria", 2.3), self.settings.calls[-1])
        self.assertEqual("settings", w.stack.get_visible_child_name())
        self.assertTrue(w.settings_status.get_visible())
        self.assertEqual("Настройки сохранены", w.settings_status.get_text())
        self.assertEqual({"success"}, css_classes(w.settings_status))

    def test_duck_spin_range_and_label(self) -> None:
        spin = self.window.duck_spin
        self.assertEqual((0.0, 100.0), spin.get_range())
        self.assertEqual(5.0, spin.get_increments()[0])
        self.assertEqual(0, spin.get_digits())
        labels = []

        def collect(widget) -> None:
            if isinstance(widget, Gtk.Label):
                labels.append(widget.get_text())
            if isinstance(widget, Gtk.Container):
                widget.forall(collect)

        collect(self.window.stack.get_child_by_name("settings"))
        self.assertIn("Приглушать другие звуки до, %", labels)

    def test_duck_spin_reloaded_on_each_open(self) -> None:
        w = self.window
        self.prefs.loaded = Prefs(tail=7, autoplay=True, duck=80)
        w.settings_button.clicked()
        self.assertEqual(80, w.duck_spin.get_value_as_int())
        w.duck_spin.set_value(10)
        w.back_button.clicked()
        w.settings_button.clicked()
        self.assertEqual(80, w.duck_spin.get_value_as_int())

    def test_save_takes_typed_duck_without_focus_loss(self) -> None:
        w = self.window
        w.settings_button.clicked()
        w.duck_spin.set_text("45")
        w.save_button.clicked()
        self.assertEqual(45, self.prefs.calls[-1][1].duck)

    def test_save_keeps_loaded_duck_when_untouched(self) -> None:
        w = self.window
        self.prefs.loaded = Prefs(tail=7, autoplay=True, duck=65)
        w.settings_button.clicked()
        w.save_button.clicked()
        self.assertEqual(Prefs(tail=7, autoplay=True, duck=65), self.prefs.calls[-1][1])

    def test_save_error_keeps_screen_and_shows_reason(self) -> None:
        w = self.window
        w.settings_button.clicked()
        self.settings.save_error = SettingsError("не удалось записать")
        w.save_button.clicked()
        self.assertEqual("settings", w.stack.get_visible_child_name())
        self.assertEqual("не удалось записать", w.settings_status.get_text())
        self.assertEqual({"error"}, css_classes(w.settings_status))

    def test_prefs_save_error_shows_reason(self) -> None:
        w = self.window
        w.settings_button.clicked()
        self.prefs.save_error = SettingsError("settings.json не записан")
        w.save_button.clicked()
        self.assertEqual("settings.json не записан", w.settings_status.get_text())
        self.assertEqual({"error"}, css_classes(w.settings_status))
        self.assertEqual([], self.player.calls)
        self.assertEqual([], [call for call in self.settings.calls if call[0] == "save"])

    def test_voice_save_error_after_prefs_saved_keeps_order(self) -> None:
        w = self.window
        w.settings_button.clicked()
        self.settings.save_error = SettingsError("не удалось записать")
        w.save_button.clicked()
        # хвост уже записан и применён к плееру до сбоя записи голоса
        prefs_saves = [call for call in self.prefs.calls if call[0] == "save"]
        self.assertEqual(1, len(prefs_saves))
        self.assertEqual([("apply_prefs", prefs_saves[0][1])], self.player.calls)
        self.assertEqual(1, len([call for call in self.settings.calls if call[0] == "save"]))
        self.assertEqual("не удалось записать", w.settings_status.get_text())

    def test_reopen_hides_result(self) -> None:
        w = self.window
        w.settings_button.clicked()
        w.save_button.clicked()
        self.assertEqual({"success"}, css_classes(w.settings_status))
        w.back_button.clicked()
        w.settings_button.clicked()
        self.assertFalse(w.settings_status.get_visible())
        self.assertEqual(set(), css_classes(w.settings_status))

    def test_status_hidden_after_show_all(self) -> None:
        w = self.window
        w.settings_button.clicked()
        w.show_all()
        flush()
        self.assertFalse(w.settings_status.get_visible())

    def test_back_returns_without_save(self) -> None:
        w = self.window
        w.settings_button.clicked()
        w.voice_combo.set_active_id("aria")
        w.back_button.clicked()
        self.assertEqual("main", w.stack.get_visible_child_name())
        self.assertEqual([("load",)], self.settings.calls)


class TextScreenTest(unittest.TestCase):
    """Экран «Свой текст» (контракт (10)) поверх FakeTextSpeaker."""

    def setUp(self) -> None:
        self.player = FakePlayer(state("paused", None, []))
        self.text = FakeTextSpeaker()
        self.window = PlayerWindow(self.player, FakeSettings(voice_settings()),
                                   FakePrefs(Prefs(tail=7, autoplay=True)),
                                   self.text)
        self.addCleanup(self.window.destroy)

    def calls(self, name: str) -> list[tuple]:
        return [call for call in self.text.calls if call[0] == name]

    def test_text_button_icon_tooltip_after_end_session(self) -> None:
        w = self.window
        button = w.text_button
        self.assertFalse(button.get_label())
        self.assertEqual("document-edit-symbolic", icon_name(button))
        self.assertEqual("Озвучить свой текст", button.get_tooltip_text())
        row = w.clear_button.get_parent()
        self.assertEqual([w.settings_button, w.clear_button, w.end_session_button, button],
                         row.get_children()[:4])
        self.assertEqual(Gtk.PackType.START, row.child_get_property(button, "pack-type"))

    def test_click_opens_text_screen(self) -> None:
        w = self.window
        w.text_button.clicked()
        self.assertEqual("text", w.stack.get_visible_child_name())
        self.assertIs(w.stack.get_child_by_name("text"), w.stack.get_visible_child())

    def test_text_screen_order_top_to_bottom(self) -> None:
        w = self.window
        screen = w.stack.get_child_by_name("text")
        header = w.text_back_button.get_parent()
        scrolled = w.text_view.get_parent()
        self.assertIsInstance(scrolled, Gtk.ScrolledWindow)
        transport = w.text_play_button.get_parent()
        self.assertIs(transport, w.text_position.get_parent())
        self.assertEqual([header, scrolled, w.text_scale, w.text_error, transport],
                         screen.get_children())
        self.assertTrue(screen.child_get_property(scrolled, "expand"))
        self.assertEqual("go-previous-symbolic", icon_name(w.text_back_button))
        self.assertEqual(Gtk.WrapMode.WORD_CHAR, w.text_view.get_wrap_mode())
        # время слева, play справа — как в ряду главного экрана
        self.assertEqual(Gtk.PackType.START,
                         transport.child_get_property(w.text_position, "pack-type"))
        self.assertEqual(Gtk.PackType.END,
                         transport.child_get_property(w.text_play_button, "pack-type"))

    def test_typing_sets_text_from_buffer(self) -> None:
        buffer = self.window.text_view.get_buffer()
        buffer.set_text("Привет. Мир.")
        self.assertEqual(("set_text", "Привет. Мир."), self.calls("set_text")[-1])
        buffer.set_text("")
        self.assertEqual(("set_text", ""), self.calls("set_text")[-1])

    def test_play_button_toggles_and_disabled_when_empty(self) -> None:
        w = self.window
        self.assertFalse(w.text_play_button.get_sensitive())
        self.text.change(text_state("stopped"))
        self.assertTrue(w.text_play_button.get_sensitive())
        w.text_play_button.clicked()
        self.assertEqual([("toggle_play_pause",)], self.text.calls)
        self.assertEqual([], self.player.calls)

    def test_play_label_and_slider_follow_state(self) -> None:
        w = self.window
        cases = (
            (text_state("synthesizing"), "⏸", True, False),
            (text_state("playing", 2.0, 5.0), "⏸", True, True),
            (text_state("paused", 3.0, 5.0), "⏵", True, True),
            (text_state("stopped", 0.0, 5.0), "⏵", True, True),
            (text_state("error", 0.0, 0.0, "процесс синтеза завершился"), "⏵", True, False),
            (text_state("empty"), "⏵", False, False),
        )
        for snapshot, label, enabled, slider in cases:
            self.text.change(snapshot)
            self.assertEqual(label, w.text_play_button.get_label(), snapshot.status)
            self.assertEqual(enabled, w.text_play_button.get_sensitive(), snapshot.status)
            self.assertEqual(slider, w.text_scale.get_sensitive(), snapshot.status)
            self.assertEqual(snapshot.position_s, w.text_scale.get_value(), snapshot.status)
            adjustment = w.text_scale.get_adjustment()
            self.assertEqual(snapshot.length_s, adjustment.get_upper(), snapshot.status)
        self.text.change(text_state("playing", 65.0, 125.0))
        self.assertEqual("1:05 / 2:05", w.text_position.get_text())
        self.assertEqual([], self.calls("seek"), "обновление из состояния не перематывает")

    def test_error_text_under_slider_while_error_set(self) -> None:
        w = self.window
        w.show_all()
        flush()
        self.assertFalse(w.text_error.get_visible())
        self.text.change(text_state("error", error="процесс синтеза остановлен сигналом 9"))
        self.assertTrue(w.text_error.get_visible())
        self.assertEqual("процесс синтеза остановлен сигналом 9", w.text_error.get_text())
        # готовое доигрывает после сбоя синтеза: статус playing, ошибка уже есть
        self.text.change(text_state("playing", 1.0, 3.0, "синтез: нет голоса"))
        self.assertTrue(w.text_error.get_visible())
        self.assertEqual("синтез: нет голоса", w.text_error.get_text())
        self.text.change(text_state("stopped", 0.0, 3.0))
        self.assertFalse(w.text_error.get_visible())

    def test_user_slider_change_seeks_text(self) -> None:
        w = self.window
        self.text.change(text_state("paused", 0.0, 10.0))
        w.text_scale.emit("change-value", Gtk.ScrollType.JUMP, 4.0)
        self.assertEqual([("seek", 4.0)], self.calls("seek"))
        self.assertEqual([], self.player.calls)

    def test_text_drag_seeks_once_on_release(self) -> None:
        w = self.window
        self.text.change(text_state("playing", 1.0, 10.0))
        w.text_slider.press()
        w.text_scale.emit("change-value", Gtk.ScrollType.JUMP, 4.0)
        w.text_scale.emit("change-value", Gtk.ScrollType.JUMP, 6.0)
        self.text.change(text_state("playing", 2.0, 10.0))
        self.assertEqual(6.0, w.text_scale.get_value(), "пока тянут, состояние не двигает")
        self.assertEqual([], self.calls("seek"))
        w.text_slider.release()
        self.assertEqual([("seek", 6.0)], self.calls("seek"))

    def test_back_pauses_playing_text(self) -> None:
        w = self.window
        w.text_button.clicked()
        self.text.change(text_state("playing", 1.0, 5.0))
        w.text_back_button.clicked()
        self.assertEqual("main", w.stack.get_visible_child_name())
        self.assertEqual([("toggle_play_pause",)], self.text.calls)
        self.assertEqual("paused", self.text.state().status)

    def test_back_pauses_synthesizing_text(self) -> None:
        w = self.window
        w.text_button.clicked()
        self.text.change(text_state("synthesizing"))
        w.text_back_button.clicked()
        self.assertEqual("main", w.stack.get_visible_child_name())
        self.assertEqual([("toggle_play_pause",)], self.text.calls)

    def test_back_leaves_silent_text_alone(self) -> None:
        w = self.window
        for status in ("empty", "paused", "stopped", "error"):
            self.text.calls.clear()
            w.text_button.clicked()
            self.text.change(text_state(status))
            w.text_back_button.clicked()
            self.assertEqual("main", w.stack.get_visible_child_name(), status)
            self.assertEqual([], self.text.calls, status)

    def test_main_play_with_silent_text_toggles_only_player(self) -> None:
        w = self.window
        self.text.change(text_state("paused", 1.0, 5.0))
        w.play_pause_button.clicked()
        self.assertEqual([], self.text.calls)
        self.assertEqual([("toggle_play_pause",)], self.player.calls)

    def test_refresh_tick_updates_text_position(self) -> None:
        """Позиция играющего своего текста сама on_change не шлёт: её двигает тик окна."""
        w = self.window
        self.text.snapshot = text_state("playing", 3.0, 10.0)
        w._refresh()
        self.assertEqual(3.0, w.text_scale.get_value())


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
