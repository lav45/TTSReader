"""Окно плеера и значок в трее. Что показывать — решает tts_player.view над player.state();
команды уходят в Player только через его методы."""
import logging
from dataclasses import replace
from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

try:  # значок XApp — в Cinnamon, MATE, Xfce; без библиотеки (macOS) — Gtk.StatusIcon
    gi.require_version("XApp", "1.0")
    from gi.repository import XApp  # noqa: E402
except (ValueError, ImportError):
    XApp = None

from tts_player.daemon_settings import DaemonSettings, Voice  # noqa: E402
from tts_player.model import Message  # noqa: E402
from tts_player.player import PlayerState  # noqa: E402
from tts_player.player_settings import PlayerSettings, Prefs  # noqa: E402
from tts_player.settings_error import SettingsError  # noqa: E402
from tts_player.text_speaker import SOUNDING_STATUSES, TextSpeaker, TextState  # noqa: E402
from tts_player.view import (MessageRow, SessionRow, clear_enabled,  # noqa: E402
                             play_pause_enabled, play_pause_label, position_label,
                             stop_enabled, text_play_enabled,
                             text_play_pause_label, text_position_label, tree_rows)

log = logging.getLogger(__name__)

APP_ICON = "audio-x-generic"  # один значок у трея и окна: панель задач берёт значок окна
FOLDER_OPEN_ARROW = "▾"
FOLDER_CLOSED_ARROW = "▸"
FOLDER_OPEN_ICON = "folder-open-symbolic"
FOLDER_CLOSED_ICON = "folder-symbolic"
SAVED_TEXT = "Настройки сохранены"
STATUS_SUCCESS = "success"
STATUS_ERROR = "error"
STATUS_BAR = "bar"  # полоса во всю ширину окна у нижнего края
SCREEN_MARGIN = 12
TEXT_PADDING = 8  # текст в поле «Свой текст» не прижат к рамке
# блок результата на экране настроек: провайдер ставится только на этот Label
STATUS_CSS = b"""
label.success, label.error { padding: 6px 8px; border-radius: 4px; }
label.success { background-color: alpha(#2e7d32, 0.25); border: 1px solid alpha(#2e7d32, 0.4); }
label.error { color: #ffffff; background-color: #c62828; border: 1px solid #8e0000; }
label.bar { padding: 3px 12px; border-radius: 0; border-width: 1px 0 0 0; }
"""


EMPTY_RANGE_S = 1.0  # диапазон ползунка без длины: бегунок виден в начале полосы


class SeekSlider:
    """Ползунок позиции с перемоткой: сдвиг пользователем — seek(значение); при перетаскивании —
    один seek по отпусканию и только если двигали. Пока тянут, show() ползунок не двигает.
    Один на главном экране (Player.seek) и один на экране «Свой текст» (TextSpeaker.seek)."""

    def __init__(self, seek: Callable[[float], None]) -> None:
        self._seek = seek
        self._dragging = False
        self._drag_value: float | None = None  # change-value за перетаскивание; None — не двигали
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 1.0)
        self.scale.set_draw_value(False)
        self.scale.connect("button-press-event", lambda _scale, _event: self.press())
        self.scale.connect("button-release-event", lambda _scale, _event: self.release())
        self.scale.connect("change-value", self._on_change)
        self.scale.connect("grab-broken-event", lambda _scale, _event: self.end_drag())

    def show(self, position_s: float, length_s: float) -> None:
        """Позиция из состояния; set_range/set_value не шлют change-value — перемотки нет.
        Длины нет — диапазон EMPTY_RANGE_S: у пустого диапазона GTK не рисует ни бегунок, ни
        полосу, и ползунок пропадает."""
        if self._dragging:
            return
        self.scale.set_range(0.0, length_s if length_s > 0 else EMPTY_RANGE_S)
        self.scale.set_value(position_s)

    def press(self) -> bool:
        self._dragging = True
        self._drag_value = None
        return False

    def release(self) -> bool:
        """seek — только если ползунок двигали: щелчок на месте не перематывает."""
        value = self._drag_value
        self.end_drag()
        if value is not None:
            self._seek(value)
        return False

    def end_drag(self) -> bool:
        """Отпускание не придёт (grab-broken, окно спрятано): ползунок снова за состоянием."""
        self._dragging = False
        self._drag_value = None
        return False

    def _on_change(self, _scale, _scroll, value: float) -> bool:
        """Пользователь сдвинул ползунок; при перетаскивании seek — по отпусканию."""
        if self._dragging:
            self._drag_value = value
        else:
            self._seek(value)
        return False


class PlayerWindow(Gtk.Window):
    """Три экрана в одном Gtk.Stack.

    "main" сверху вниз: ряд служебных значков — слева «Настройки», «Очистить сообщения
    активной сессии» (активна, пока есть активная сессия), справа «Озвучить свой текст»; дерево сессий — папки (открыта одна или ни одной: открытая —
    активная), дорожки открытой — под её заголовком с отступом; ползунок позиции; ряд со
    временем слева, play/pause и ⏩ автовоспроизведения справа (⏩ — крайняя справа).
    "settings": хвост очереди (PlayerSettings), голос и скорость claude-tts
    (DaemonSettings); «Сохранить» пишет их и остаётся на экране, итог — в блоке внизу; стрелка
    назад возвращает без записи. Пауза других плееров и её тишина (pause_others_available) и
    пауза на диктовку (dictation_available) показаны, только если механизм есть: MPRIS по D-Bus,
    pactl в Linux. "text": свой текст (TextSpeaker) — поле ввода, ползунок, ошибка,
    время и play/pause; стрелка назад ставит звучащий свой текст на паузу, мусорка «Очистить»
    стирает поле.

    Один звук за раз: play/pause главного экрана при звучащем своём тексте сначала ставит его
    на паузу. Обновляется по on_change плеера и TextSpeaker и, пока окно видно, таймером
    REFRESH_MS — позиция играющего файла сама on_change не вызывает. Закрытие прячет окно в трей.
    """
    REFRESH_MS = 250
    SAVED_HIDE_MS = 2000  # «Настройки сохранены» гаснет само; ошибка остаётся до закрытия экрана
    TRACK_INDENT = 28  # отступ дорожки от левого края: под значком папки
    SPEED_STEP = 0.1
    SPEED_DIGITS = 2  # 1.25 из config не округляется спином при потере фокуса
    GAP_STEP_MS = 100

    def __init__(self, player, settings: DaemonSettings, prefs: PlayerSettings,
                 text_speaker: TextSpeaker, *, pause_others_available: bool = True,
                 dictation_available: bool = True) -> None:
        super().__init__(title="TTS Player")
        self._pause_others_available = pause_others_available
        self._dictation_available = dictation_available
        self._player = player
        self._settings = settings
        self._prefs = prefs
        self._text_speaker = text_speaker
        self._voice_loaded = False  # голос и скорость прочитаны при открытии настроек
        self._refresh_id = 0
        self._status_hide_id = 0
        self._shown_rows: list[SessionRow | MessageRow] | None = None
        # сообщения последнего состояния: строки равны по тексту, а объекты Message могут быть
        # новыми, поэтому снимок обновляется на каждом состоянии, а не только при перестройке
        self._shown_messages: list[Message] = []
        self.set_default_size(420, 360)
        self.set_icon_name(APP_ICON)

        self.slider = SeekSlider(player.seek)
        self.scale = self.slider.scale
        self.position_label_widget = Gtk.Label(xalign=0.0)

        self.stop_button = self._button("⏹", player.stop)
        self.play_pause_button = self._button("", self._toggle_main)
        self.clear_button = self._icon_button(
            "user-trash-symbolic", "Очистить сообщения активной сессии", player.clear)
        self.settings_button = self._icon_button("emblem-system-symbolic", "Настройки",
                                                 self._open_settings)
        self.text_button = self._icon_button("text-x-generic-symbolic", "Озвучить свой текст",
                                             self._open_text)

        self.autoplay_button = Gtk.ToggleButton()
        self.autoplay_button.set_image(
            Gtk.Image.new_from_icon_name("media-seek-forward-symbolic", Gtk.IconSize.BUTTON))
        self.autoplay_button.set_tooltip_text(
            "Автовоспроизведение: следующая дорожка и новые ответы")
        self._autoplay_handler = self.autoplay_button.connect("toggled", self._on_autoplay_toggled)

        # время слева, справа play/pause и за ним ⏩ автовоспроизведения (крайняя справа)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        buttons.pack_start(self.position_label_widget, False, False, 0)
        buttons.pack_end(self.autoplay_button, False, False, 0)
        buttons.pack_end(self.play_pause_button, False, False, 0)
        buttons.pack_end(self.stop_button, False, False, 0)

        self.tree = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.tree.set_activate_on_single_click(True)
        self.tree.connect("row-activated", self._on_row_activated)
        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.add(self.tree)

        # ряд служебных значков наверху: «Свой текст» прижат к правому краю
        tools = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        for button in (self.settings_button, self.clear_button):
            tools.pack_start(button, False, False, 0)
        tools.pack_end(self.text_button, False, False, 0)

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=SCREEN_MARGIN)
        main.pack_start(tools, False, False, 0)
        main.pack_start(scrolled, True, True, 0)
        main.pack_start(self.scale, False, False, 0)
        main.pack_start(buttons, False, False, 0)

        self.stack = Gtk.Stack()
        self.stack.add_named(main, "main")
        self.stack.add_named(self._settings_screen(), "settings")
        self.stack.add_named(self._text_screen(), "text")
        self.add(self.stack)
        self.stack.show_all()
        self.stack.set_visible_child_name("main")

        self.connect("delete-event", self._on_delete)
        self.connect("show", self._on_show)
        self.connect("hide", self._on_hide)
        player.on_change = self._show_state
        text_speaker.on_change = self._show_text
        self._show_state(player.state())
        self._show_text(text_speaker.state())

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
        """Экран настроек: стрелка назад, голос, скорость, хвост, флажок паузы других плееров,
        пауза до и после речи, продолжение после диктовки, «Сохранить», блок итога."""
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
        self.pause_others_check = Gtk.CheckButton(label="Ставить другие плееры на паузу")
        self.gap_spin = Gtk.SpinButton.new_with_range(
            PlayerSettings.GAP_MIN_MS, PlayerSettings.GAP_MAX_MS, self.GAP_STEP_MS)
        self.gap_spin.set_digits(0)
        self.dictation_gap_spin = Gtk.SpinButton.new_with_range(
            PlayerSettings.DICTATION_GAP_MIN_MS, PlayerSettings.DICTATION_GAP_MAX_MS,
            self.GAP_STEP_MS)
        self.dictation_gap_spin.set_digits(0)
        self.save_button = self._button("Сохранить", self._save_settings)
        self.settings_status = self._status_label()
        self.settings_status.get_style_context().add_class(STATUS_BAR)

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
        # скрытые поля не прикреплены, но значения из файла держат: «Сохранить» их не теряет
        if self._pause_others_available:
            fields.attach(self.pause_others_check, 0, 3, 2, 1)
            fields.attach(Gtk.Label(label="Пауза до и после речи, мс", xalign=0.0), 0, 4, 1, 1)
            fields.attach(self.gap_spin, 1, 4, 1, 1)
        if self._dictation_available:
            dictation_gap_label = Gtk.Label(label="Продолжать после диктовки через, мс", xalign=0.0)
            dictation_gap_label.set_line_wrap(True)  # длинная подпись не растягивает окно
            fields.attach(dictation_gap_label, 0, 5, 1, 1)
            fields.attach(self.dictation_gap_spin, 1, 5, 1, 1)
        self.voice_combo.set_hexpand(True)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        actions.pack_end(self.save_button, False, False, 0)

        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=SCREEN_MARGIN)
        form.pack_start(header, False, False, 0)
        form.pack_start(fields, False, False, 0)
        form.pack_start(actions, False, False, 0)

        # блок итога вне отступа формы: полоса во всю ширину у нижнего края окна
        screen = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        screen.pack_start(form, False, False, 0)
        screen.pack_end(self.settings_status, False, False, 0)
        return screen

    @staticmethod
    def _status_label() -> Gtk.Label:
        """Блок итога: скрыт, пока пуст; цвет — класс success или error (STATUS_CSS)."""
        label = Gtk.Label(xalign=0.0, wrap=True, selectable=True)
        label.set_no_show_all(True)  # show_all окна не открывает пустой блок
        provider = Gtk.CssProvider()
        provider.load_from_data(STATUS_CSS)
        label.get_style_context().add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        return label

    def _text_screen(self) -> Gtk.Box:
        """Экран «Свой текст»: стрелка назад, мусорка «Очистить», поле ввода, ползунок, ошибка,
        время и play/pause."""
        self.text_back_button = self._icon_button("go-previous-symbolic", "Назад",
                                                  self._leave_text)
        self.text_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR,  # длинное слово — по буквам
                                      left_margin=TEXT_PADDING, right_margin=TEXT_PADDING,
                                      top_margin=TEXT_PADDING, bottom_margin=TEXT_PADDING)
        self.text_view.get_buffer().connect("changed", self._on_text_changed)
        self.text_slider = SeekSlider(self._text_speaker.seek)
        self.text_scale = self.text_slider.scale
        self.text_error = self._status_label()
        self.text_error.get_style_context().add_class(STATUS_ERROR)
        self.text_play_button = self._button("", self._text_speaker.toggle_play_pause)
        self.text_position = Gtk.Label(xalign=0.0)
        self.text_clear_button = self._icon_button("user-trash-symbolic", "Очистить",
                                                   self._clear_text)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.pack_start(self.text_back_button, False, False, 0)
        header.pack_start(Gtk.Label(label="Свой текст", xalign=0.0), False, False, 0)
        header.pack_end(self.text_clear_button, False, False, 0)

        # EXTERNAL, а не NEVER: при NEVER поле требует от окна ширину, до которой его однажды
        # растянул перенесённый текст, и окно перестаёт сужаться; строки и так переносятся
        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.EXTERNAL)
        scrolled.set_shadow_type(Gtk.ShadowType.IN)
        scrolled.add(self.text_view)

        # как ряд главного экрана: время слева, кнопка справа
        transport = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        transport.pack_start(self.text_position, False, False, 0)
        transport.pack_end(self.text_play_button, False, False, 0)

        screen = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=SCREEN_MARGIN)
        screen.pack_start(header, False, False, 0)
        screen.pack_start(scrolled, True, True, 0)
        screen.pack_start(self.text_scale, False, False, 0)
        screen.pack_start(self.text_error, False, False, 0)
        screen.pack_start(transport, False, False, 0)
        return screen

    def _open_text(self) -> None:
        self.stack.set_visible_child_name("text")
        self.text_view.grab_focus()

    def _clear_text(self) -> None:
        """Мусорка экрана: поле пустеет — через «changed» свой текст останавливается и
        становится empty, как при стирании руками; курсор остаётся в поле."""
        self.text_view.get_buffer().set_text("")
        self.text_view.grab_focus()

    def _leave_text(self) -> None:
        """Стрелка назад: звучащий свой текст встаёт на паузу. Из synthesizing — тоже paused:
        синтез идёт дальше, а готовое предложение не начинает звучать само; продолжает
        пользователь кнопкой play на этом экране."""
        self._pause_text()
        self._show_main()

    def _pause_text(self) -> None:
        if self._text_speaker.state().status in SOUNDING_STATUSES:
            self._text_speaker.toggle_play_pause()

    def _toggle_main(self) -> None:
        """Play/pause главного экрана. Главный экран виден, только когда свой текст молчит
        (инвариант держит _leave_text), поэтому его кнопки и клик по строке свой текст не
        трогают."""
        self._player.toggle_play_pause()

    def _on_autoplay_toggled(self, button: Gtk.ToggleButton) -> None:
        """Клик пользователя по ⏩: положение пишется в файл плеера и применяется к плееру.
        Сбой записи — WARNING в лог, режим всё равно применяется: до перезапуска плеер ведёт
        себя как показано на кнопке. PlayerSettings.load не бросает (на порче — умолчания)."""
        prefs = replace(self._prefs.load(), autoplay=button.get_active())
        try:
            self._prefs.save(prefs)
        except SettingsError as e:
            log.warning("Автовоспроизведение не сохранено: %s", e)
        self._player.apply_prefs(prefs)

    def _show_autoplay(self, active: bool) -> None:
        """Показ состояния плеера: свой обработчик заблокирован, это не клик пользователя."""
        self.autoplay_button.handler_block(self._autoplay_handler)
        self.autoplay_button.set_active(active)
        self.autoplay_button.handler_unblock(self._autoplay_handler)

    def _on_text_changed(self, buffer: Gtk.TextBuffer) -> None:
        start, end = buffer.get_bounds()
        self._text_speaker.set_text(buffer.get_text(start, end, False))

    def _show_text(self, state: TextState) -> None:
        self.text_play_button.set_label(text_play_pause_label(state))
        self.text_play_button.set_sensitive(text_play_enabled(state))
        self.text_clear_button.set_sensitive(state.status != "empty")
        self.text_position.set_text(text_position_label(state))
        self.text_scale.set_sensitive(state.length_s > 0)
        self.text_slider.show(state.position_s, state.length_s)
        # ошибка бывает и при playing/paused: готовое доигрывает после сбоя синтеза
        self.text_error.set_text(state.error)
        self.text_error.set_visible(bool(state.error))

    def _open_settings(self) -> None:
        """Каждый вход читает настройки заново: несохранённое с прошлого раза не остаётся.
        Хвост, флажок паузы, пауза до и после речи и продолжение после диктовки — из файла плеера и доступны всегда; голос и скорость — только если
        настройки claude-tts прочитались."""
        self._hide_status()
        loaded_prefs = self._prefs.load()
        self.tail_spin.set_value(loaded_prefs.tail)
        self.pause_others_check.set_active(loaded_prefs.pause_others)
        self.gap_spin.set_value(loaded_prefs.gap_ms)
        self.dictation_gap_spin.set_value(loaded_prefs.dictation_gap_ms)
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
        """Сначала хвост, флажок паузы, пауза до и после речи и продолжение после диктовки в файл плеера и в плеер, затем
        голос и скорость, если они прочитались. Экран остаётся, итог — в блоке внизу."""
        try:
            self.tail_spin.update()  # набранный текст без потери фокуса
            self.gap_spin.update()
            self.dictation_gap_spin.update()
            prefs = Prefs(tail=self.tail_spin.get_value_as_int(),
                          autoplay=self.autoplay_button.get_active(),
                          pause_others=self.pause_others_check.get_active(),
                          gap_ms=self.gap_spin.get_value_as_int(),
                          dictation_gap_ms=self.dictation_gap_spin.get_value_as_int())
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
        self._cancel_status_hide()
        self._clear_status_classes().add_class(kind)
        self.settings_status.set_text(text)
        self.settings_status.show()
        if kind == STATUS_SUCCESS:
            self._status_hide_id = GLib.timeout_add(self.SAVED_HIDE_MS, self._on_status_timeout)

    def _hide_status(self) -> None:
        self._cancel_status_hide()
        self._clear_status_classes()
        self.settings_status.set_text("")
        self.settings_status.hide()

    def _cancel_status_hide(self) -> None:
        if self._status_hide_id:
            GLib.source_remove(self._status_hide_id)
            self._status_hide_id = 0

    def _on_status_timeout(self) -> bool:
        self._status_hide_id = 0  # источник снимает SOURCE_REMOVE, remove не нужен
        self._hide_status()
        return GLib.SOURCE_REMOVE

    def _show_main(self) -> None:
        self.stack.set_visible_child_name("main")

    def _show_state(self, state: PlayerState) -> None:
        self.position_label_widget.set_text(position_label(state))
        self.play_pause_button.set_label(play_pause_label(state))
        self.play_pause_button.set_sensitive(play_pause_enabled(state))
        self.stop_button.set_sensitive(stop_enabled(state))
        self.scale.set_sensitive(state.current is not None)
        self.slider.show(state.position_s, state.length_s)
        self.clear_button.set_sensitive(clear_enabled(state))
        self._show_autoplay(state.autoplay)
        self._shown_messages = state.messages
        self._show_rows(tree_rows(state))

    def _on_row_activated(self, _list, row: Gtk.ListBoxRow) -> None:
        """Клик по заголовку закрытой папки открывает её сессию, открытой — закрывает; клик по
        дорожке сразу играет её."""
        item = self._shown_rows[row.get_index()]
        if isinstance(item, MessageRow):
            self._player.play_message(self._shown_messages[item.index])
        elif item.open:
            self._player.close_session()
        else:
            self._player.select_session(item.id)

    def _show_rows(self, items: list[SessionRow | MessageRow]) -> None:
        """Дерево перестраивается, только когда строки изменились, а не на каждый тик."""
        if items == self._shown_rows:
            return
        self._shown_rows = items
        focused = self.tree.get_focus_child()  # строка с фокусом клавиатуры или None
        focused_index = None if focused is None else focused.get_index()
        for child in self.tree.get_children():
            self.tree.remove(child)
        for item in items:
            line = self._message_row(item) if isinstance(item, MessageRow) else self._folder_row(item)
            self.tree.add(line)
        self.tree.show_all()
        if focused_index is not None and items:
            self._restore_focus(items, focused_index)

    def _restore_focus(self, items: list[SessionRow | MessageRow], focused_index: int) -> None:
        """Перестройка уничтожила строку с фокусом: фокус — на строку текущего сообщения, без
        него — на заголовок открытой папки, без неё — на ту же позицию (не дальше последней),
        чтобы стрелки и Enter работали."""
        current = [i for i, item in enumerate(items) if isinstance(item, MessageRow) and item.current]
        opened = [i for i, item in enumerate(items) if isinstance(item, SessionRow) and item.open]
        index = (current or opened or [min(focused_index, len(items) - 1)])[0]
        self.tree.get_row_at_index(index).grab_focus()

    @staticmethod
    def _bold(label: Gtk.Label) -> None:
        attributes = Pango.AttrList()
        attributes.insert(Pango.attr_weight_new(Pango.Weight.BOLD))
        label.set_attributes(attributes)

    def _folder_row(self, item: SessionRow) -> Gtk.Box:
        """▾/▸, значок папки, «проект · id» (открытая — жирным)."""
        arrow = Gtk.Label(label=FOLDER_OPEN_ARROW if item.open else FOLDER_CLOSED_ARROW)
        icon = Gtk.Image.new_from_icon_name(
            FOLDER_OPEN_ICON if item.open else FOLDER_CLOSED_ICON, Gtk.IconSize.MENU)
        title = Gtk.Label(label=item.title, xalign=0.0, ellipsize=Pango.EllipsizeMode.END)
        if item.open:
            self._bold(title)
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, margin=4)
        line.pack_start(arrow, False, False, 0)
        line.pack_start(icon, False, False, 0)
        line.pack_start(title, True, True, 0)
        return line

    def _message_row(self, item: MessageRow) -> Gtk.Box:
        """С отступом под заголовком папки: первые слова, справа время и длительность."""
        title = Gtk.Label(label=item.title, xalign=0.0, ellipsize=Pango.EllipsizeMode.END)
        time_label = Gtk.Label(label=item.time, xalign=1.0)
        duration = Gtk.Label(label=item.duration, xalign=1.0)
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, margin=4,
                       margin_start=self.TRACK_INDENT)
        line.pack_start(title, True, True, 0)
        line.pack_end(duration, False, False, 0)
        line.pack_end(time_label, False, False, 0)
        for label in (title, time_label, duration):
            if item.current:
                self._bold(label)
            if item.heard:
                label.get_style_context().add_class(Gtk.STYLE_CLASS_DIM_LABEL)
        return line

    @property
    def refreshing(self) -> bool:
        """Таймер обновления позиции запущен (окно видно)."""
        return self._refresh_id != 0

    def _on_delete(self, _window, _event) -> bool:
        self.hide()
        return True

    def _on_show(self, _window) -> None:
        self._show_state(self._player.state())
        self._show_text(self._text_speaker.state())
        if not self._refresh_id:
            self._refresh_id = GLib.timeout_add(self.REFRESH_MS, self._refresh)

    def _on_hide(self, _window) -> None:
        self.slider.end_drag()
        self.text_slider.end_drag()
        if self._refresh_id:
            GLib.source_remove(self._refresh_id)
            self._refresh_id = 0

    def _refresh(self) -> bool:
        self._show_state(self._player.state())
        self._show_text(self._text_speaker.state())
        return GLib.SOURCE_CONTINUE


def tray_menu(quit_app: Callable[[], None]) -> Gtk.Menu:
    """Меню правого клика по значку: «Выход» — quit_app (выход из главного цикла)."""
    menu = Gtk.Menu()
    item = Gtk.MenuItem(label="Выход")
    item.connect("activate", lambda _item: quit_app())
    menu.append(item)
    menu.show_all()
    return menu


def tray_click(window: Gtk.Window, button: int, time: int) -> None:
    """Левый клик по значку: окно показывается и встаёт поверх остальных, даже если уже открыто.

    Время клика нужно оконному менеджеру: без него запрет перехвата фокуса оставляет окно позади.
    """
    if button != Gdk.BUTTON_PRIMARY:
        return
    window.present_with_time(time)


class TrayIcon:
    """Значок в трее: левый клик поднимает окно поверх остальных, правый — меню с «Выход».

    XApp.StatusIcon, если библиотека есть (Cinnamon, MATE, Xfce); иначе Gtk.StatusIcon — в macOS
    он в строке меню."""
    NAME = "tts-player"
    TOOLTIP = "TTS Player"

    def __init__(self, window: Gtk.Window, quit_app: Callable[[], None]) -> None:
        self._window = window
        self._menu = tray_menu(quit_app)
        if XApp is not None:
            self._icon = XApp.StatusIcon()
            self._icon.set_name(self.NAME)
            self._icon.set_icon_name(APP_ICON)
            self._icon.set_tooltip_text(self.TOOLTIP)
            self._icon.set_secondary_menu(self._menu)
            self._icon.connect("activate", self._on_activate)
            self._icon.set_visible(True)
            return
        self._icon = Gtk.StatusIcon.new_from_icon_name(APP_ICON)
        self._icon.set_title(self.TOOLTIP)
        self._icon.set_tooltip_text(self.TOOLTIP)
        self._icon.connect("activate", self._on_gtk_activate)
        self._icon.connect("popup-menu", self._on_gtk_popup)
        self._icon.set_visible(True)

    def _on_activate(self, _icon, button: int, time: int) -> None:
        tray_click(self._window, button, time)

    def _on_gtk_activate(self, _icon) -> None:
        tray_click(self._window, Gdk.BUTTON_PRIMARY, Gtk.get_current_event_time())

    def _on_gtk_popup(self, icon, button: int, time: int) -> None:
        self._menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, time)
