"""Окно плеера и значок в трее. Что показывать — решает tts_player.view над player.state();
команды уходят в Player только через его методы."""
import logging
from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("XApp", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, XApp  # noqa: E402

from tts_player.daemon_settings import DaemonSettings, Voice  # noqa: E402
from tts_player.model import Message  # noqa: E402
from tts_player.player import PlayerState  # noqa: E402
from tts_player.player_settings import PlayerSettings, Prefs  # noqa: E402
from tts_player.settings_error import SettingsError  # noqa: E402
from tts_player.voicing import Voicing  # noqa: E402
from tts_player.view import (MessageRow, next_enabled, play_pause_enabled,  # noqa: E402
                             play_pause_label, position_label, prev_enabled, rows)

log = logging.getLogger(__name__)

PREV_LABEL = "⏮"
NEXT_LABEL = "⏭"
SAVED_TEXT = "Настройки сохранены"
STATUS_SUCCESS = "success"
STATUS_ERROR = "error"
# блок результата на экране настроек: провайдер ставится только на этот Label
STATUS_CSS = b"""
label.success, label.error { padding: 6px 8px; border-radius: 4px; color: #ffffff; }
label.success { background-color: #2e7d32; border: 1px solid #1b5e20; }
label.error { background-color: #c62828; border: 1px solid #8e0000; }
"""


class PlayerWindow(Gtk.Window):
    """Два экрана в одном Gtk.Stack.

    "main" сверху вниз: ряд служебных значков — слева «Очистить буфер», «Настройки» и
    «Выключить озвучку» (активен, пока озвучка включена; состояние читается при показе окна
    и на тике таймера), справа выключатель автовоспроизведения; список сообщений; ползунок
    позиции; ряд prev / play-pause / next со временем справа. "settings": хвост очереди
    (PlayerSettings), голос и скорость claude-tts (DaemonSettings); «Сохранить» пишет их и
    остаётся на экране, итог — в блоке внизу; стрелка назад возвращает без записи.

    Обновляется по player.on_change и, пока окно видно, таймером REFRESH_MS — позиция
    играющего файла сама on_change не вызывает. Закрытие прячет окно в трей.
    """
    REFRESH_MS = 250
    SPEED_STEP = 0.1
    SPEED_DIGITS = 2  # 1.25 из config не округляется спином при потере фокуса

    def __init__(self, player, settings: DaemonSettings, prefs: PlayerSettings,
                 voicing: Voicing) -> None:
        super().__init__(title="TTS Player")
        self._player = player
        self._settings = settings
        self._prefs = prefs
        self._voicing = voicing
        self._voice_loaded = False  # голос и скорость прочитаны при открытии настроек
        self._refresh_id = 0
        self._dragging = False
        self._drag_value: float | None = None  # change-value за перетаскивание; None — не двигали
        self._shown_rows: list[MessageRow] | None = None
        # сообщения последнего состояния: строки равны по тексту, а объекты Message могут быть
        # новыми, поэтому снимок обновляется на каждом состоянии, а не только при перестройке
        self._shown_messages: list[Message] = []
        self.set_default_size(420, 360)

        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 1.0)
        self.scale.set_draw_value(False)
        self.scale.connect("button-press-event", self._on_scale_press)
        self.scale.connect("button-release-event", self._on_scale_release)
        self.scale.connect("change-value", self._on_scale_change)
        self.scale.connect("grab-broken-event", self._on_scale_grab_broken)
        self.position_label_widget = Gtk.Label(xalign=1.0)

        self.prev_button = self._button(PREV_LABEL, player.prev)
        self.play_pause_button = self._button("", player.toggle_play_pause)
        self.next_button = self._button(NEXT_LABEL, player.next)
        self.clear_button = self._icon_button("user-trash-symbolic", "Очистить буфер",
                                              player.clear)
        self.settings_button = self._icon_button("emblem-system-symbolic", "Настройки",
                                                 self._open_settings)
        self.voicing_off_button = self._icon_button(
            "system-shutdown-symbolic", "Выключить озвучку (/tts-off)", self._turn_voicing_off)
        self.autoplay_button = Gtk.ToggleButton(image=Gtk.Image.new_from_icon_name(
            "media-playlist-consecutive-symbolic", Gtk.IconSize.BUTTON))
        self.autoplay_button.set_relief(Gtk.ReliefStyle.NONE)
        self.autoplay_button.set_tooltip_text("Автовоспроизведение")
        self._autoplay_handler = self.autoplay_button.connect("toggled", self._on_autoplay_toggled)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for button in (self.prev_button, self.play_pause_button, self.next_button):
            buttons.pack_start(button, False, False, 0)
        buttons.pack_end(self.position_label_widget, False, False, 0)

        self.message_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.message_list.set_activate_on_single_click(True)
        self.message_list.connect("row-activated", self._on_row_activated)
        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.add(self.message_list)

        # ряд служебных значков наверху: команды слева, выключатели справа
        tools = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        for button in (self.clear_button, self.settings_button, self.voicing_off_button):
            tools.pack_start(button, False, False, 0)
        tools.pack_end(self.autoplay_button, False, False, 0)

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        main.pack_start(tools, False, False, 0)
        main.pack_start(scrolled, True, True, 0)
        main.pack_start(self.scale, False, False, 0)
        main.pack_start(buttons, False, False, 0)

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
        self._show_voicing()

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
        """Экран настроек: стрелка назад, голос, скорость, хвост, «Сохранить», блок итога."""
        self.back_button = self._icon_button("go-previous-symbolic", "Назад", self._show_main)
        self.voice_combo = Gtk.ComboBoxText()
        for renderer in self.voice_combo.get_cells():
            # длинное «имя — описание» обрезается, а не растягивает окно
            renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        self.speed_spin = Gtk.SpinButton.new_with_range(
            DaemonSettings.SPEED_MIN, DaemonSettings.SPEED_MAX, self.SPEED_STEP)
        self.speed_spin.set_digits(self.SPEED_DIGITS)
        self.tail_spin = Gtk.SpinButton.new_with_range(
            PlayerSettings.TAIL_MIN, PlayerSettings.TAIL_MAX, 1)
        self.tail_spin.set_digits(0)
        self.save_button = self._button("Сохранить", self._save_settings)
        self.settings_status = Gtk.Label(xalign=0.0, wrap=True, selectable=True)
        self.settings_status.set_no_show_all(True)  # show_all окна не открывает пустой блок
        provider = Gtk.CssProvider()
        provider.load_from_data(STATUS_CSS)
        self.settings_status.get_style_context().add_provider(
            provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.pack_start(self.back_button, False, False, 0)
        header.pack_start(Gtk.Label(label="Настройки", xalign=0.0), False, False, 0)

        fields = Gtk.Grid(row_spacing=6, column_spacing=12)
        fields.attach(Gtk.Label(label="Голос", xalign=0.0), 0, 0, 1, 1)
        fields.attach(self.voice_combo, 1, 0, 1, 1)
        fields.attach(Gtk.Label(label="Скорость", xalign=0.0), 0, 1, 1, 1)
        fields.attach(self.speed_spin, 1, 1, 1, 1)
        fields.attach(Gtk.Label(label="Хвост очереди", xalign=0.0), 0, 2, 1, 1)
        fields.attach(self.tail_spin, 1, 2, 1, 1)
        self.voice_combo.set_hexpand(True)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        actions.pack_end(self.save_button, False, False, 0)

        screen = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        screen.pack_start(header, False, False, 0)
        screen.pack_start(fields, False, False, 0)
        screen.pack_start(actions, False, False, 0)
        screen.pack_end(self.settings_status, False, False, 0)  # узкий блок внизу экрана
        return screen

    def _open_settings(self) -> None:
        """Каждый вход читает настройки заново: несохранённое с прошлого раза не остаётся.
        Хвост — из файла плеера и доступен всегда; голос и скорость — только если
        настройки claude-tts прочитались."""
        self._hide_status()
        self.tail_spin.set_value(self._prefs.load().tail)
        self.voice_combo.remove_all()
        try:
            loaded = self._settings.load()
        except SettingsError as e:
            self._set_voice_enabled(False)
            self._show_status(str(e), STATUS_ERROR)
        else:
            for voice in loaded.voices:
                self.voice_combo.append(voice.name, self._voice_text(voice))
            self.voice_combo.set_active_id(loaded.active)
            self.speed_spin.set_value(loaded.speed)
            self._set_voice_enabled(True)
        self.stack.set_visible_child_name("settings")

    @staticmethod
    def _voice_text(voice: Voice) -> str:
        if voice.description:
            return f"{voice.name} — {voice.description}"
        return voice.name

    def _set_voice_enabled(self, enabled: bool) -> None:
        self._voice_loaded = enabled
        for widget in (self.voice_combo, self.speed_spin):
            widget.set_sensitive(enabled)

    def _save_settings(self) -> None:
        """Сначала хвост (с текущим автовоспроизведением) в файл плеера и в плеер, затем
        голос и скорость, если они прочитались. Экран остаётся, итог — в блоке внизу."""
        try:
            self.tail_spin.update()  # набранный текст без потери фокуса
            # autoplay здесь не правится: берём текущее значение плеера (его же показывает
            # кнопка), хвост — из спина
            prefs = Prefs(tail=self.tail_spin.get_value_as_int(),
                          autoplay=self._player.state().autoplay)
            self._prefs.save(prefs)
            self._player.apply_prefs(prefs)
            if self._voice_loaded:
                self.speed_spin.update()
                # стрелка даёт 1.9000000000000001: округляем до знаков спина
                speed = round(self.speed_spin.get_value(), self.SPEED_DIGITS)
                self._settings.save(self.voice_combo.get_active_id(), speed)
        except SettingsError as e:
            self._show_status(str(e), STATUS_ERROR)
            return
        self._show_status(SAVED_TEXT, STATUS_SUCCESS)

    def _clear_status_classes(self) -> Gtk.StyleContext:
        context = self.settings_status.get_style_context()
        for name in (STATUS_SUCCESS, STATUS_ERROR):
            context.remove_class(name)
        return context

    def _show_status(self, text: str, kind: str) -> None:
        self._clear_status_classes().add_class(kind)
        self.settings_status.set_text(text)
        self.settings_status.show()

    def _hide_status(self) -> None:
        self._clear_status_classes()
        self.settings_status.set_text("")
        self.settings_status.hide()

    def _on_autoplay_toggled(self, button: Gtk.ToggleButton) -> None:
        """Переключение пользователем: хвост — из файла плеера, autoplay — новое значение.
        Сбой записи — в лог, кнопка возвращается к состоянию плеера."""
        # хвост здесь не правится: берём сохранённый в файле (спин мог быть набран, но не сохранён),
        # autoplay — из кнопки
        prefs = Prefs(tail=self._prefs.load().tail, autoplay=button.get_active())
        try:
            self._prefs.save(prefs)
        except SettingsError as e:
            log.warning("автовоспроизведение не сохранено: %s", e)
            self._show_autoplay(self._player.state().autoplay)
            return
        self._player.apply_prefs(prefs)

    def _show_autoplay(self, autoplay: bool) -> None:
        """Кнопка следует за состоянием, не вызывая сохранения."""
        with self.autoplay_button.handler_block(self._autoplay_handler):
            self.autoplay_button.set_active(autoplay)

    def _show_main(self) -> None:
        self.stack.set_visible_child_name("main")

    def _show_state(self, state: PlayerState) -> None:
        self.position_label_widget.set_text(position_label(state))
        self.play_pause_button.set_label(play_pause_label(state))
        self.play_pause_button.set_sensitive(play_pause_enabled(state))
        self._show_autoplay(state.autoplay)
        self.prev_button.set_sensitive(prev_enabled(state))
        self.next_button.set_sensitive(next_enabled(state))
        self.scale.set_sensitive(state.current is not None)
        if not self._dragging:
            # set_range/set_value не шлют change-value: перемотки от обновления нет
            self.scale.set_range(0.0, state.length_s)
            self.scale.set_value(state.position_s)
        self._shown_messages = state.messages
        self._show_rows(rows(state))

    def _on_row_activated(self, _list, row: Gtk.ListBoxRow) -> None:
        """Клик по строке: сообщение i-й строки выбирается (играет по play/pause)."""
        self._player.play_message(self._shown_messages[row.get_index()])

    def _show_rows(self, items: list[MessageRow]) -> None:
        """Список перестраивается, только когда строки изменились, а не на каждый тик."""
        if items == self._shown_rows:
            return
        self._shown_rows = items
        focused = self.message_list.get_focus_child()  # строка с фокусом клавиатуры или None
        focused_index = None if focused is None else focused.get_index()
        for child in self.message_list.get_children():
            self.message_list.remove(child)
        for item in items:
            self.message_list.add(self._row(item))
        self.message_list.show_all()
        if focused_index is not None:
            self._restore_focus(items, focused_index)

    def _restore_focus(self, items: list[MessageRow], focused_index: int) -> None:
        """Перестройка уничтожила строку с фокусом: фокус — на строку текущего сообщения,
        без текущего — на ту же позицию (не дальше последней), чтобы стрелки и Enter работали."""
        current = [index for index, item in enumerate(items) if item.current]
        index = current[0] if current else min(focused_index, len(items) - 1)
        row = self.message_list.get_row_at_index(index)
        if row is not None:
            row.grab_focus()

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
        self._show_voicing()
        if not self._refresh_id:
            self._refresh_id = GLib.timeout_add(self.REFRESH_MS, self._refresh)

    def _on_hide(self, _window) -> None:
        self._end_drag()
        if self._refresh_id:
            GLib.source_remove(self._refresh_id)
            self._refresh_id = 0

    def _refresh(self) -> bool:
        self._show_state(self._player.state())
        self._show_voicing()
        return GLib.SOURCE_CONTINUE

    def _turn_voicing_off(self) -> None:
        self._voicing.turn_off()
        # гасим сразу: двойной клик не запускает второй хук; если флаг остался, тик вернёт кнопку
        self.voicing_off_button.set_sensitive(False)

    def _show_voicing(self) -> None:
        """Флаг озвучки читается с диска, поэтому не на каждое событие плеера, а на показ и тик."""
        self.voicing_off_button.set_sensitive(self._voicing.is_on())


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
