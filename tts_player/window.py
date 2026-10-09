"""Окно плеера и значок в трее. Что показывать — решает tts_player.view над player.state();
команды уходят в Player только через его методы."""
from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("XApp", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, XApp  # noqa: E402

from tts_player.daemon_settings import DaemonSettings, SettingsError, Voice  # noqa: E402
from tts_player.player import PlayerState  # noqa: E402
from tts_player.view import (MessageRow, current_title, next_enabled, pause_enabled,  # noqa: E402
                             pause_label, play_stop_label, position_label, prev_enabled, rows)

PREV_LABEL = "⏮"
NEXT_LABEL = "⏭"


class PlayerWindow(Gtk.Window):
    """Два экрана в одном Gtk.Stack.

    "main": кнопки prev / pause / play|stop / next, ползунок позиции, список сообщений,
    внизу слева значки «Очистить буфер» и «Настройки». "settings": голос и скорость
    claude-tts через DaemonSettings; «Сохранить» пишет их и возвращает на главный экран,
    стрелка назад возвращает без записи.

    Обновляется по player.on_change и, пока окно видно, таймером REFRESH_MS — позиция
    играющего файла сама on_change не вызывает. Закрытие прячет окно в трей.
    """
    REFRESH_MS = 250
    SPEED_STEP = 0.1
    SPEED_DIGITS = 1

    def __init__(self, player, settings: DaemonSettings) -> None:
        super().__init__(title="TTS Player")
        self._player = player
        self._settings = settings
        self._refresh_id = 0
        self._dragging = False
        self._drag_value: float | None = None  # change-value за перетаскивание; None — не двигали
        self._shown_rows: list[MessageRow] | None = None
        self.set_default_size(420, 360)

        self.title_label = Gtk.Label(xalign=0.0, ellipsize=Pango.EllipsizeMode.END)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 1.0)
        self.scale.set_draw_value(False)
        self.scale.connect("button-press-event", self._on_scale_press)
        self.scale.connect("button-release-event", self._on_scale_release)
        self.scale.connect("change-value", self._on_scale_change)
        self.scale.connect("grab-broken-event", self._on_scale_grab_broken)
        self.position_label_widget = Gtk.Label(xalign=1.0)

        self.prev_button = self._button(PREV_LABEL, player.prev)
        self.pause_button = self._button("", player.toggle_pause)
        self.play_stop_button = self._button("", player.toggle_play_stop)
        self.next_button = self._button(NEXT_LABEL, player.next)
        self.clear_button = self._icon_button("user-trash-symbolic", "Очистить буфер",
                                              player.clear)
        self.settings_button = self._icon_button("emblem-system-symbolic", "Настройки",
                                                 self._open_settings)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for button in (self.prev_button, self.pause_button, self.play_stop_button,
                       self.next_button):
            buttons.pack_start(button, False, False, 0)
        buttons.pack_end(self.position_label_widget, False, False, 0)

        self.message_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.add(self.message_list)

        # ряд значков внизу слева; следующие значки — pack_start сюда же
        tools = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        for button in (self.clear_button, self.settings_button):
            tools.pack_start(button, False, False, 0)

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        main.pack_start(self.title_label, False, False, 0)
        main.pack_start(self.scale, False, False, 0)
        main.pack_start(buttons, False, False, 0)
        main.pack_start(scrolled, True, True, 0)
        main.pack_start(tools, False, False, 0)

        self.stack = Gtk.Stack(margin=12)
        self.stack.add_named(main, "main")
        self.stack.add_named(self._settings_screen(), "settings")
        self.add(self.stack)
        self.stack.show_all()
        self.stack.set_visible_child_name("main")

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

    @staticmethod
    def _icon_button(icon: str, tooltip: str, command) -> Gtk.Button:
        """Значок без текста; что делает — в подсказке."""
        button = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_tooltip_text(tooltip)
        button.connect("clicked", lambda _button: command())
        return button

    def _settings_screen(self) -> Gtk.Box:
        """Экран настроек: стрелка назад, голос, скорость, «Сохранить», причина сбоя."""
        self.back_button = self._icon_button("go-previous-symbolic", "Назад", self._show_main)
        self.voice_combo = Gtk.ComboBoxText()
        self.speed_spin = Gtk.SpinButton.new_with_range(
            DaemonSettings.SPEED_MIN, DaemonSettings.SPEED_MAX, self.SPEED_STEP)
        self.speed_spin.set_digits(self.SPEED_DIGITS)
        self.save_button = self._button("Сохранить", self._save_settings)
        self.settings_error = Gtk.Label(xalign=0.0, wrap=True, selectable=True)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.pack_start(self.back_button, False, False, 0)
        header.pack_start(Gtk.Label(label="Настройки", xalign=0.0), False, False, 0)

        fields = Gtk.Grid(row_spacing=6, column_spacing=12)
        fields.attach(Gtk.Label(label="Голос", xalign=0.0), 0, 0, 1, 1)
        fields.attach(self.voice_combo, 1, 0, 1, 1)
        fields.attach(Gtk.Label(label="Скорость", xalign=0.0), 0, 1, 1, 1)
        fields.attach(self.speed_spin, 1, 1, 1, 1)
        self.voice_combo.set_hexpand(True)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        actions.pack_end(self.save_button, False, False, 0)

        screen = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        screen.pack_start(header, False, False, 0)
        screen.pack_start(fields, False, False, 0)
        screen.pack_start(actions, False, False, 0)
        screen.pack_start(self.settings_error, False, False, 0)
        return screen

    def _open_settings(self) -> None:
        """Каждый вход читает настройки заново: несохранённое с прошлого раза не остаётся."""
        self.voice_combo.remove_all()
        try:
            loaded = self._settings.load()
        except SettingsError as e:
            self._set_settings_enabled(False)
            self.settings_error.set_text(str(e))
        else:
            for voice in loaded.voices:
                self.voice_combo.append(voice.name, self._voice_text(voice))
            self.voice_combo.set_active_id(loaded.active)
            self.speed_spin.set_value(loaded.speed)
            self._set_settings_enabled(True)
            self.settings_error.set_text("")
        self.stack.set_visible_child_name("settings")

    @staticmethod
    def _voice_text(voice: Voice) -> str:
        if voice.description:
            return f"{voice.name} — {voice.description}"
        return voice.name

    def _set_settings_enabled(self, enabled: bool) -> None:
        for widget in (self.voice_combo, self.speed_spin, self.save_button):
            widget.set_sensitive(enabled)

    def _save_settings(self) -> None:
        try:
            self._settings.save(self.voice_combo.get_active_id(), self.speed_spin.get_value())
        except SettingsError as e:
            self.settings_error.set_text(str(e))
            return
        self._show_main()

    def _show_main(self) -> None:
        self.stack.set_visible_child_name("main")

    def _show_state(self, state: PlayerState) -> None:
        self.title_label.set_text(current_title(state))
        self.position_label_widget.set_text(position_label(state))
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
        for child in self.message_list.get_children():
            self.message_list.remove(child)
        for item in items:
            self.message_list.add(self._row(item))
        self.message_list.show_all()

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

    @property
    def refreshing(self) -> bool:
        """Таймер обновления позиции запущен (окно видно)."""
        return self._refresh_id != 0

    def _on_scale_press(self, _scale, _event) -> bool:
        self._dragging = True
        self._drag_value = None
        return False

    def _on_scale_release(self, _scale, _event) -> bool:
        """seek — только если ползунок двигали: щелчок на месте не перематывает."""
        value = self._drag_value
        self._end_drag()
        if value is not None:
            self._player.seek(value)
        return False

    def _on_scale_change(self, _scale, _scroll, value: float) -> bool:
        """Пользователь сдвинул ползунок; при перетаскивании seek — по отпусканию."""
        if self._dragging:
            self._drag_value = value
        else:
            self._player.seek(value)
        return False

    def _on_scale_grab_broken(self, _scale, _event) -> bool:
        """Отпускание кнопки не придёт: перетаскивание отменено, ползунок снова за состоянием."""
        self._end_drag()
        return False

    def _end_drag(self) -> None:
        self._dragging = False
        self._drag_value = None

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
