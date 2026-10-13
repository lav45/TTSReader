"""Свой текст (контракт (10)): синтез процессом synth_worker по предложениям, проигрывание
готовых подряд, удержание главного плеера на время звучания."""
import json
import logging
import os
import shutil
import signal
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from tts_player.dictation_gate import DictationGate  # noqa: E402
from tts_player.output_idle import OutputIdle  # noqa: E402
from tts_player.playback import Playback  # noqa: E402
from tts_player.player import Player  # noqa: E402
from tts_player.synth_worker import EXIT_IMPORT_FAILED  # noqa: E402

log = logging.getLogger("tts_player.text_speaker")

TextStatus = Literal["empty", "synthesizing", "playing", "paused", "stopped", "error"]
# свой текст звучит или вот-вот зазвучит: в этих статусах главный плеер в удержании, а стрелка
# назад и команда stop ставят текст на паузу
SOUNDING_STATUSES: tuple[TextStatus, ...] = ("playing", "synthesizing")


@dataclass(frozen=True)
class TextState:
    """Снимок для экрана «Свой текст». empty — текста нет; synthesizing — play нажат, первое
    предложение ещё не готово; error — синтез или воспроизведение не удались, текст в error."""
    status: TextStatus
    position_s: float
    length_s: float  # сумма duration пришедших предложений
    error: str


@dataclass(frozen=True)
class _Ready:
    """Синтезированное предложение: файл и длительность из строки процесса."""
    path: Path
    duration: float


class _Synthesis:
    """Один запуск процесса синтеза: текст — в stdin, строки stdout — в on_line, выход — в
    on_exit(процесс), нечитаемый stdout — в on_broken(текст). Чтение неблокирующее, в главном
    цикле GLib; процесс подбирает Gio.Subprocess. После stop колбэков нет."""
    STOP_TIMEOUT_S = 2.0  # после SIGTERM — столько ждать выхода, затем SIGKILL

    def __init__(self, argv: list[str], text: str, on_line: Callable[[str], None],
                 on_exit: Callable[[Gio.Subprocess], None],
                 on_broken: Callable[[str], None]) -> None:
        """GLib.Error — процесс не запустился."""
        self._on_line = on_line
        self._on_exit = on_exit
        self._on_broken = on_broken
        flags = Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE
        self._process = Gio.Subprocess.new(argv, flags)
        self._cancellable = Gio.Cancellable()
        data = GLib.Bytes.new(text.encode("utf-8"))
        source = Gio.MemoryInputStream.new_from_bytes(data)
        splice_flags = (Gio.OutputStreamSpliceFlags.CLOSE_SOURCE
                        | Gio.OutputStreamSpliceFlags.CLOSE_TARGET)
        stdin = self._process.get_stdin_pipe()
        stdin.splice_async(source, splice_flags, GLib.PRIORITY_DEFAULT, self._cancellable,
                           self._written)
        self._stdout = Gio.DataInputStream.new(self._process.get_stdout_pipe())
        self._read_next()

    def stop(self) -> None:
        """Колбэки сняты; живой процесс (вся его группа) получает SIGTERM, выход ждётся здесь
        не дольше STOP_TIMEOUT_S, затем SIGKILL. Срок отмеряет поток threading.Timer: wait() крутит свой
        контекст GLib, таймер главного цикла в нём не сработает."""
        self._cancellable.cancel()
        if self._process.get_identifier() is None:
            return
        self._signal(signal.SIGTERM)
        deadline = Gio.Cancellable()
        timer = threading.Timer(self.STOP_TIMEOUT_S, deadline.cancel)
        timer.start()
        try:
            self._process.wait(deadline)
            return
        except GLib.Error:
            log.warning("процесс синтеза не вышел за %.1f с после SIGTERM, SIGKILL",
                        self.STOP_TIMEOUT_S)
        finally:
            timer.cancel()
        self._signal(signal.SIGKILL)
        self._process.wait(None)

    def _signal(self, number: signal.Signals) -> None:
        """Сигнал группе процесса: synth_worker — лидер своей группы, и sherpa/piper/ffmpeg
        под ним умирают вместе с ним. Группы ещё нет (воркер не дошёл до setpgid) — сигнал
        самому процессу."""
        pid = int(self._process.get_identifier())
        try:
            os.killpg(pid, number)
        except ProcessLookupError:
            self._process.send_signal(number)

    def _written(self, stream: Gio.OutputStream, result: Gio.AsyncResult) -> None:
        try:
            stream.splice_finish(result)
        except GLib.Error as error:
            log.debug("текст в stdin процесса синтеза не записан: %s", error.message)

    def _read_next(self) -> None:
        self._stdout.read_line_async(GLib.PRIORITY_DEFAULT, self._cancellable, self._read)

    def _read(self, stream: Gio.DataInputStream, result: Gio.AsyncResult) -> None:
        try:
            line, _ = stream.read_line_finish_utf8(result)
        except GLib.Error as error:
            if not self._cancellable.is_cancelled():
                self._on_broken(f"вывод процесса синтеза не прочитан: {error.message}")
            return
        if line is None:
            self._process.wait_async(self._cancellable, self._exited)
            return
        self._on_line(line)
        if not self._cancellable.is_cancelled():
            self._read_next()

    def _exited(self, process: Gio.Subprocess, result: Gio.AsyncResult) -> None:
        try:
            process.wait_finish(result)
        except GLib.Error:
            return  # stop: выход ждёт сам stop
        self._on_exit(process)


class TextSpeaker:
    """Свой текст: процесс синтеза пишет предложения в <runtime>/tts-player/text/<n>/,
    готовые играют подряд своим Playback, пока остальные синтезируются.

    Удержание главного плеера решает одно место — _changed: статус synthesizing или
    playing — player.hold(), иначе player.release(). Паузу других плееров ведёт главный
    Player тем же MprisPause: в удержании он звучит для других (Player._speaking), поэтому
    своему тексту отдельного владельца паузы не нужно; ушедший в hold из playing главный
    плеер паузу не повторяет, а тишины перед звуком (gap_ms) у своего текста нет.

    Сбой синтеза ({"error"}, выход ≠ 0 без done, процесс не запустился) — текст в error;
    звучащее готовое доигрывает, и статус error ставится, когда звучать нечему. Готовое
    после сбоя играет снова без нового процесса; без готового play запускает синтез заново.

    Ворота диктовки (dictation, DictationGate, общие с главным Player): закрыты — _start
    звука не пускает, а запоминает предложение и позицию (_due); статус playing (или
    synthesizing до первого готового), поэтому главный плеер остаётся в удержании, как при
    звуке. Закрылись при звуке — звук стоп, запомнено начало того же предложения (файл —
    одно предложение). Открылись при статусе playing — запомненное звучит. ⏸ за закрытыми
    воротами — пауза на начале прерванного предложения, открытие её не снимает. После
    shutdown смена ворот ничего не делает.
    """

    def __init__(self, runtime_dir: Path, python: Path, worker: Path, player: Player,
                 audio_sink: str | None = None, output: OutputIdle | None = None,
                 dictation: DictationGate | None = None) -> None:
        """dictation — ворота диктовки, None — без них; владелец — процесс, здесь только
        подписка."""
        self.on_change: Callable[[TextState], None] = lambda state: None
        self._text_root = Path(runtime_dir) / "tts-player" / "text"
        self._python = python
        self._worker = worker
        self._player = player
        self._playback = Playback(audio_sink, output)
        self._playback.on_finished = self._on_finished
        self._playback.on_error = self._on_playback_error
        self._text = ""
        self._status: TextStatus = "empty"
        self._holding = False  # player.hold() вызван, release() ещё нет
        self._serial = 0  # номер последнего каталога <n>
        self._dir: Path | None = None
        self._run: _Synthesis | None = None
        self._sentences: list[_Ready] = []
        self._complete = False
        self._synth_failed = False
        self._error = ""
        # Без файла — курсор; с файлом — позиция, с которой запущен файл _index.
        self._position = 0.0
        self._index: int | None = None
        self._file_base = 0.0
        # Предложение, которому пора звучать, а ворота диктовки закрыты; позиция — _position
        self._due: int | None = None
        self._dictation = dictation
        self._shut = False  # после shutdown смена ворот диктовки ничего не делает
        if dictation is not None:
            dictation.subscribe(self._on_dictation)

    def state(self) -> TextState:
        return TextState(self._status, self._now(), self._length(), self._error)

    def set_text(self, text: str) -> None:
        """Текст изменился: синтез отменён, звук стоп, каталог удалён; пустой — empty, иначе stopped."""
        if text == self._text:
            return
        self._text = text
        self._discard()
        self._status = self._rest_status()
        self._changed()

    def toggle_play_pause(self) -> None:
        """synthesizing/playing → paused (синтез идёт дальше); paused → с места; stopped и
        error — готовое с курсора (в конце — с начала), нет готового и синтеза — новый
        процесс; empty — ничего."""
        if self._status in SOUNDING_STATUSES:
            self._position = self._now()
            self._halt()
            self._status = "paused"
        elif self._status == "empty":
            return
        elif self._needs_synthesis():
            self._synthesize()
        else:
            position = self._position
            if self._over() and position >= self._length():
                position = 0.0
            if not self._synth_failed:
                self._error = ""
            self._play_from(position)
        self._changed()

    def seek(self, position_s: float) -> None:
        """Позиция в пределах уже синтезированного; статус не меняется."""
        position = min(max(position_s, 0.0), self._length())
        if self._status == "playing":
            self._halt()
            self._play_from(position)
        else:
            self._position = position
        self._changed()

    def shutdown(self) -> None:
        """Выход: синтез отменён, звук стоп, каталог удалён, главный плеер отпущен.

        Вызывается до Player.shutdown(): release() после него прошёл бы через Player._changed
        уже остановленного плеера — пауза других плееров (_follow_status: таймер продолжения
        _resume_later поверх уже остановленного MprisPause) и on_change окна."""
        self._shut = True
        self._discard()
        self._status = self._rest_status()
        self._holding = False
        self._player.release()

    def cleanup_leftovers(self) -> None:
        """При старте: удалить остатки прошлых запусков в <runtime>/tts-player/text/."""
        if not self._text_root.is_dir():
            return
        for entry in self._text_root.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)

    def _synthesize(self) -> None:
        """Новый процесс синтеза в новый каталог <n>; прежнее готовое сброшено."""
        self._discard()
        self._serial += 1
        while (self._text_root / str(self._serial)).exists():
            self._serial += 1
        out_dir = self._text_root / str(self._serial)
        # -P: каталог воркера не в sys.path, иначе tts_player/queue.py затенит stdlib queue
        argv = [str(self._python), "-P", str(self._worker), str(out_dir)]
        try:
            out_dir.mkdir(parents=True)
        except OSError as error:
            self._fail(f"каталог синтеза не создан: {error}")
            return
        self._dir = out_dir
        try:
            self._run = _Synthesis(argv, self._text, self._on_line, self._on_exit,
                                   self._on_broken)
        except GLib.Error as error:
            self._fail(f"процесс синтеза не запустился: {error.message}")
            return
        log.info("synthesis %s", out_dir)
        self._status = "synthesizing"

    def _on_line(self, line: str) -> None:
        message = self._parse(line)
        if message is None:
            self._stop_run()
            self._fail(f"непонятная строка процесса синтеза: {line!r}")
        elif "index" in message:
            self._on_ready(_Ready(Path(message["path"]), float(message["duration"])))
        elif "done" in message:
            self._complete = True
        elif "error" in message:
            self._fail(str(message["error"]))
        self._changed()

    def _on_broken(self, text: str) -> None:
        """stdout процесса не читается: как непонятная строка — процесс остановлен, error."""
        self._stop_run()
        self._fail(text)
        self._changed()

    def _parse(self, line: str) -> dict | None:
        """Строка протокола (10): count, следующее по порядку index, done или error; иначе None."""
        try:
            message = json.loads(line)
        except ValueError:
            return None
        if not isinstance(message, dict):
            return None
        if "index" in message:
            in_order = message["index"] == len(self._sentences)
            path_ok = isinstance(message.get("path"), str)
            duration = message.get("duration")
            duration_ok = isinstance(duration, (int, float)) and not isinstance(duration, bool)
            return message if in_order and path_ok and duration_ok else None
        if message.get("done") is True or "error" in message or "count" in message:
            return message
        return None

    def _on_ready(self, sentence: _Ready) -> None:
        self._sentences.append(sentence)
        if self._status in SOUNDING_STATUSES and self._index is None:
            self._play_from(self._position)

    def _on_exit(self, process: Gio.Subprocess) -> None:
        self._run = None
        exited = process.get_if_exited()
        code = process.get_exit_status() if exited else None
        if code == EXIT_IMPORT_FAILED and not self._complete:
            self._fail(f"процесс синтеза завершился с кодом {code} без строки error")
            self._error = f"синтез недоступен: {self._error}"
            self._changed()
            return
        if self._complete or self._synth_failed:
            return
        if exited:
            self._fail(f"процесс синтеза завершился с кодом {code} без строки done")
        else:
            self._fail(f"процесс синтеза остановлен сигналом {process.get_term_sig()}")
        self._changed()

    def _fail(self, text: str) -> None:
        """Синтез не удался: текст в error; статус error, если звучать нечему."""
        if self._synth_failed:
            return
        log.info("synthesis failed: %s", text)
        self._synth_failed = True
        self._error = text
        sounding = self._index is not None or self._due is not None
        if not sounding and (self._status != "paused" or not self._sentences):
            self._status = "error"
            self._position = 0.0

    def _on_finished(self, path: Path) -> None:
        following = self._index + 1
        self._index = None
        if following < len(self._sentences):
            self._start(following, self._offset(following))
        else:
            self._run_out()
        self._changed()

    def _on_playback_error(self, path: Path, text: str, output_failed: bool) -> None:
        log.info("error %s: %s", path.name, text)
        self._position = self._file_base
        self._index = None
        self._status = "error"
        self._error = f"воспроизведение {path.name}: {text}"
        self._changed()

    def _play_from(self, position: float) -> None:
        """Файл, содержащий позицию; позиция за готовым — ждать следующее или конец."""
        for index in range(len(self._sentences)):
            offset = self._offset(index)
            if position < offset + self._sentences[index].duration:
                self._start(index, position)
                return
        self._position = position
        self._run_out()

    def _start(self, index: int, position: float) -> None:
        """Единственное место, где звучит Playback; за закрытыми воротами — только запомнить."""
        sentence = self._sentences[index]
        self._file_base = self._offset(index)
        self._position = position
        self._status = "playing"
        if self._dictating():
            self._index = None
            self._due = index
            return
        self._index = index
        self._due = None
        log.info("playing %s", sentence.path.name)
        self._playback.play(sentence.path, start_s=position - self._file_base)

    def _dictating(self) -> bool:
        """Ворота диктовки закрыты: звука сейчас нельзя."""
        return self._dictation is not None and self._dictation.closed()

    def _on_dictation(self) -> None:
        """Ворота диктовки сменились. Закрылись при звуке — стоп, запомнено начало того же
        предложения; открылись при playing с запомненным — оно звучит."""
        if self._shut:
            return
        if self._dictating() and self._index is not None:
            index = self._index
            self._halt()
            self._start(index, self._offset(index))
        elif not self._dictating() and self._status == "playing" and self._due is not None:
            self._start(self._due, self._position)
        self._changed()

    def _run_out(self) -> None:
        """Готовое кончилось: синтез завершён — stopped с начала, не удался — error с начала,
        идёт — ждать следующее (synthesizing без готового, иначе playing)."""
        if self._complete:
            self._status = "stopped"
            self._position = 0.0
        elif self._synth_failed:
            self._status = "error"
            self._position = 0.0
        else:
            self._status = "playing" if self._sentences else "synthesizing"
            self._position = self._length()

    def _halt(self) -> None:
        self._playback.stop()
        self._index = None
        self._due = None

    def _stop_run(self) -> None:
        if self._run is not None:
            self._run.stop()
            self._run = None

    def _discard(self) -> None:
        """Звук стоп, процесс остановлен, каталог удалён, готовое и ошибка сброшены."""
        self._halt()
        self._stop_run()
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None
        self._sentences = []
        self._complete = False
        self._synth_failed = False
        self._error = ""
        self._position = 0.0

    def _needs_synthesis(self) -> bool:
        """Для текущего текста нет готового звука и не будет: синтеза нет или он не удался."""
        if self._sentences or self._complete:
            return False
        return self._run is None or self._synth_failed

    def _over(self) -> bool:
        """Синтез больше ничего не пришлёт."""
        return self._complete or self._synth_failed

    def _rest_status(self) -> TextStatus:
        return "stopped" if self._text.strip() else "empty"

    def _offset(self, index: int) -> float:
        return sum(s.duration for s in self._sentences[:index])

    def _length(self) -> float:
        return sum(s.duration for s in self._sentences)

    def _now(self) -> float:
        if self._index is None:
            return self._position
        at = self._playback.position()
        return self._position if at is None else self._file_base + at

    def _changed(self) -> None:
        """Каждая смена статуса доходит сюда: сначала удержание главного плеера, затем экран."""
        holding = self._status in SOUNDING_STATUSES
        if holding != self._holding:
            self._holding = holding
            if holding:
                self._player.hold()
            else:
                self._player.release()
        try:
            self.on_change(self.state())
        except Exception:
            log.exception("on_change: ошибка обработчика экрана")
