"""Окно плеера и значок в трее. Что показывать — решает tts_player.view над player.state();
команды уходят в Player только через его методы."""
from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("XApp", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, XApp  # noqa: E402

from tts_player.player import PlayerState  # noqa: E402
from tts_player.view import (MessageRow, current_title, next_enabled, pause_enabled,  # noqa: E402
                             pause_label, play_stop_label, position_label, prev_enabled, rows)

PREV_LABEL = "⏮"
NEXT_LABEL = "⏭"


class PlayerWindow(Gtk.Window):
    """Кнопки prev / pause / play|stop / next, ползунок позиции, список сообщений, очистка.

    Обновляется по player.on_change и, пока окно видно, таймером REFRESH_MS — позиция
    играющего файла сама on_change не вызывает. Закрытие прячет окно в трей.
    """
    REFRESH_MS = 250

    def __init__(self, player) -> None:
        super().__init__(title="TTS Player")
        self._player = player
        self._refresh_id = 0
        self._dragging = False
        self._drag_value: float | None = None  # change-value за перетаскивание; None — не двигали
        self._shown_rows: list[MessageRow] | None = None
        self.set_default_size(420, 360)

        self.title = Gtk.Label(xalign=0.0, ellipsize=Pango.EllipsizeMode.END)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 1.0)
        self.scale.set_draw_value(False)
        self.scale.connect("button-press-event", self._on_scale_press)
        self.scale.connect("button-release-event", self._on_scale_release)
        self.scale.connect("change-value", self._on_scale_change)
        self.scale.connect("grab-broken-event", self._on_scale_grab_broken)
        self.position = Gtk.Label(xalign=1.0)

        self.prev_button = self._button(PREV_LABEL, player.prev)
        self.pause_button = self._button("", player.toggle_pause)
        self.play_stop_button = self._button("", player.toggle_play_stop)
        self.next_button = self._button(NEXT_LABEL, player.next)
        self.clear_button = self._button("Очистить буфер", player.clear)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for button in (self.prev_button, self.pause_button, self.play_stop_button,
                       self.next_button):
            buttons.pack_start(button, False, False, 0)
        buttons.pack_end(self.position, False, False, 0)

        self.rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.add(self.rows)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=12)
        box.pack_start(self.title, False, False, 0)
        box.pack_start(self.scale, False, False, 0)
        box.pack_start(buttons, False, False, 0)
        box.pack_start(scrolled, True, True, 0)
        box.pack_start(self.clear_button, False, False, 0)
        self.add(box)
        box.show_all()

        self.connect("delete-event", self._on_delete)
        self.connect("show", self._on_show)
        self.connect("hide", self._on_hide)
        player.on_change = self._show_state
        self._show_state(player.state())

    @staticmethod
    def _button(label: str, command) -> Gtk.Button:
        button = Gtk.Button(label=label)
        button.connect("clicked", lambda _button: command())
        return button

    def _show_state(self, state: PlayerState) -> None:
        self.title.set_text(current_title(state))
        self.position.set_text(position_label(state))
        self.pause_button.set_label(pause_label(state))
        self.pause_button.set_sensitive(pause_enabled(state))
        self.play_stop_button.set_label(play_stop_label(state))
        self.play_stop_button.set_sensitive(state.current is not None)
        self.prev_button.set_sensitive(prev_enabled(state))
        self.next_button.set_sensitive(next_enabled(state))
        self.scale.set_sensitive(state.current is not None)
        if not self._dragging:
            # set_range/set_value не шлют change-value: перемотки от обновления нет
            self.scale.set_range(0.0, state.length_s)
            self.scale.set_value(state.position_s)
        self._show_rows(rows(state))

    def _show_rows(self, items: list[MessageRow]) -> None:
        """Список перестраивается, только когда строки изменились, а не на каждый тик."""
        if items == self._shown_rows:
            return
        self._shown_rows = items
        for child in self.rows.get_children():
            self.rows.remove(child)
        for item in items:
            self.rows.add(self._row(item))
        self.rows.show_all()

    @staticmethod
    def _row(item: MessageRow) -> Gtk.Box:
        title = Gtk.Label(label=item.title, xalign=0.0)
        duration = Gtk.Label(label=item.duration, xalign=1.0)
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, margin=4)
        line.pack_start(title, True, True, 0)
        line.pack_end(duration, False, False, 0)
        for label in (title, duration):
            if item.current:
                attributes = Pango.AttrList()
                attributes.insert(Pango.attr_weight_new(Pango.Weight.BOLD))
                label.set_attributes(attributes)
            if item.heard:
                label.get_style_context().add_class(Gtk.STYLE_CLASS_DIM_LABEL)
        return line

    def _on_scale_press(self, _scale, _event) -> bool:
        self._dragging = True
        return False

    def _on_scale_release(self, scale, _event) -> bool:
        self._dragging = False
        self._drag_value: float | None = None  # change-value за перетаскивание; None — не двигали
        self._player.seek(scale.get_value())
        return False

    def _on_scale_change(self, _scale, _scroll, value: float) -> bool:
        """Пользователь сдвинул ползунок; при перетаскивании seek — по отпусканию."""
        if not self._dragging:
            self._player.seek(value)
        return False

    def _on_delete(self, _window, _event) -> bool:
        self.hide()
        return True

    def _on_show(self, _window) -> None:
        self._show_state(self._player.state())
        if not self._refresh_id:
            self._refresh_id = GLib.timeout_add(self.REFRESH_MS, self._refresh)

    def _on_hide(self, _window) -> None:
        self._end_drag()
        if self._refresh_id:
            GLib.source_remove(self._refresh_id)
            self._refresh_id = 0

    def _refresh(self) -> bool:
        self._show_state(self._player.state())
        return GLib.SOURCE_CONTINUE


def tray_menu(quit_app: Callable[[], None]) -> Gtk.Menu:
    """Меню правого клика по значку: «Выход» — quit_app (выход из главного цикла)."""
    menu = Gtk.Menu()
    item = Gtk.MenuItem(label="Выход")
    item.connect("activate", lambda _item: quit_app())
    menu.append(item)
    menu.show_all()
    return menu


class TrayIcon:
    """Значок XApp в трее: левый клик показывает или прячет окно, правый — меню с «Выход»."""
    NAME = "tts-player"
    ICON = "audio-x-generic"
    TOOLTIP = "TTS Player"

    def __init__(self, window: Gtk.Window, quit_app: Callable[[], None]) -> None:
        self._window = window
        self._menu = tray_menu(quit_app)
        self._icon = XApp.StatusIcon()
        self._icon.set_name(self.NAME)
        self._icon.set_icon_name(self.ICON)
        self._icon.set_tooltip_text(self.TOOLTIP)
        self._icon.set_secondary_menu(self._menu)
        self._icon.connect("activate", self._on_activate)
        self._icon.set_visible(True)

    def _on_activate(self, _icon, button: int, _time: int) -> None:
        if button != Gdk.BUTTON_PRIMARY:
            return
        if self._window.get_visible():
            self._window.hide()
        else:
            self._window.present()
