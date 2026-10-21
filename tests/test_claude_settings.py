"""Хуки плеера в settings.json Claude Code: прежние записи озвучки заменяются, чужие остаются."""
import tests  # noqa: F401 — окружение тестов (tests/__init__.py)
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tts_player.claude_settings import EVENTS, install_hooks, main

HOOK = "/home/u/.local/bin/tts-player-hook"


def commands(settings: dict, event: str) -> list[str]:
    return [h["command"] for e in settings["hooks"].get(event, []) for h in e["hooks"]]


class InstallHooksTest(unittest.TestCase):
    def test_empty_settings_get_every_event(self) -> None:
        result = install_hooks({}, HOOK)
        for event, (argument, *_rest) in EVENTS.items():
            self.assertEqual([f"{HOOK} {argument}"], commands(result, event), event)
        stop = result["hooks"]["Stop"][0]
        self.assertEqual({"matcher": "*", "hooks": [
            {"type": "command", "command": f"{HOOK} stop", "timeout": 180, "async": True}]}, stop)
        self.assertNotIn("matcher", result["hooks"]["SessionStart"][0])

    def test_question_speaks_text_before_it(self) -> None:
        """PostToolUse вопроса приходит после ответа: текст перед вопросом озвучивает PreToolUse."""
        result = install_hooks({}, HOOK)
        self.assertEqual([{"matcher": "AskUserQuestion", "hooks": [
            {"type": "command", "command": f"{HOOK} pre-tool-use", "timeout": 30, "async": True}]}],
            result["hooks"]["PreToolUse"])

    def test_previous_voice_entries_are_replaced(self) -> None:
        """Прежняя установка плеера (гейт) и установщик claude-tts: их записей не остаётся."""
        settings = {"hooks": {
            "Stop": [{"matcher": "*", "hooks": [
                {"type": "command", "command": "/home/u/.claude/hooks/tts-gate.sh speak-response.sh"}]}],
            "UserPromptSubmit": [
                {"matcher": "*", "hooks": [{"type": "command", "command": "/h/voice-context.sh"}]},
                {"matcher": "*", "hooks": [{"type": "command", "command": "/h/prompt-submitted.sh"}]}],
            "SessionStart": [{"hooks": [{"type": "command", "command": "/old/tts-player-hook start"}]}],
        }}
        result = install_hooks(settings, HOOK)
        self.assertEqual([f"{HOOK} stop"], commands(result, "Stop"))
        self.assertEqual([f"{HOOK} prompt"], commands(result, "UserPromptSubmit"))
        self.assertEqual([f"{HOOK} start"], commands(result, "SessionStart"))

    def test_foreign_hooks_and_keys_stay(self) -> None:
        settings = {"model": "opus", "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook claude"}]}],
            "Stop": [{"matcher": "*", "hooks": [
                {"type": "command", "command": "notify-send done"},
                {"type": "command", "command": "/h/speak-response.sh"}]}],
        }}
        result = install_hooks(settings, HOOK)
        self.assertEqual("opus", result["model"])
        self.assertEqual([*settings["hooks"]["PreToolUse"], {"matcher": "AskUserQuestion", "hooks": [
            {"type": "command", "command": f"{HOOK} pre-tool-use", "timeout": 30, "async": True}]}],
            result["hooks"]["PreToolUse"])
        self.assertEqual(["notify-send done", f"{HOOK} stop"], commands(result, "Stop"))
        self.assertEqual(2, len(settings["hooks"]["Stop"][0]["hooks"]), "исходный dict не меняется")

    def test_second_install_changes_nothing(self) -> None:
        once = install_hooks({}, HOOK)
        self.assertEqual(once, install_hooks(once, HOOK))


class MainTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "settings.json"

    def run_main(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = main(list(args), self.path)
        return code, out.getvalue(), err.getvalue()

    def test_writes_with_backup_and_keeps_mode_and_unicode(self) -> None:
        self.path.write_text(json.dumps({"заметка": "привет"}, ensure_ascii=False))
        self.path.chmod(0o600)
        code, out, _err = self.run_main(HOOK)
        self.assertEqual(0, code)
        self.assertIn("привет", self.path.read_text(encoding="utf-8"))
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        [backup] = self.dir.glob("settings.json.bak-*")
        self.assertEqual({"заметка": "привет"}, json.loads(backup.read_text()))
        self.assertIn("вписаны", out)

    def test_missing_file_is_created_without_backup(self) -> None:
        self.assertEqual(0, self.run_main(HOOK)[0])
        self.assertIn("SessionStart", json.loads(self.path.read_text())["hooks"])
        self.assertEqual([], list(self.dir.glob("*.bak-*")))

    def test_up_to_date_file_is_not_rewritten(self) -> None:
        self.run_main(HOOK)
        for backup in self.dir.glob("*.bak-*"):
            backup.unlink()
        code, out, _err = self.run_main(HOOK)
        self.assertEqual(0, code)
        self.assertIn("уже", out)
        self.assertEqual([], list(self.dir.glob("*.bak-*")))

    def test_broken_json_is_left_alone(self) -> None:
        self.path.write_text("{broken")
        code, _out, err = self.run_main(HOOK)
        self.assertEqual(1, code)
        self.assertEqual("{broken", self.path.read_text())
        self.assertIn("не JSON", err)

    def test_relative_hook_path_is_refused(self) -> None:
        self.assertEqual(2, self.run_main("tts-player-hook")[0])


if __name__ == "__main__":
    unittest.main()
