"""Настройки claude-tts для плеера: queue, player_command, персона Supertonic."""
import tests  # noqa: F401 — окружение тестов (tests/__init__.py)
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tts_player.claude_tts_config import SUPERTONIC, main, player_config

PLAYER = "/home/u/.local/bin/tts-player"


class PlayerConfigTest(unittest.TestCase):
    def test_fresh_config_gets_queue_sink_and_supertonic(self) -> None:
        config = {"mode": "direct", "active_persona": "claude-prime",
                  "personas": {"claude-prime": {"voice": "en"}}, "queue": {"max_depth": 20}}
        result = player_config(config, PLAYER)
        self.assertEqual("queue", result["mode"])
        self.assertEqual([PLAYER, "sink"], result["player_command"])
        self.assertEqual(SUPERTONIC, result["active_persona"])
        self.assertEqual({"claude-prime", SUPERTONIC}, set(result["personas"]))
        self.assertEqual({"max_depth": 20}, result["queue"])
        self.assertEqual("direct", config["mode"], "исходный dict не меняется")

    def test_existing_supertonic_and_active_persona_stay(self) -> None:
        config = {"active_persona": "claude-prime",
                  "personas": {SUPERTONIC: {"speed": 1.2}, "claude-prime": {}}}
        result = player_config(config, PLAYER)
        self.assertEqual("claude-prime", result["active_persona"])
        self.assertEqual({"speed": 1.2}, result["personas"][SUPERTONIC])

    def test_second_run_changes_nothing(self) -> None:
        once = player_config({}, PLAYER)
        self.assertEqual(once, player_config(once, PLAYER))


class MainTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "config.json"

    def run_main(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = main(list(args), self.path)
        return code, out.getvalue(), err.getvalue()

    def test_writes_config(self) -> None:
        self.path.write_text(json.dumps({"mode": "direct"}))
        self.assertEqual(0, self.run_main(PLAYER)[0])
        self.assertEqual("queue", json.loads(self.path.read_text())["mode"])
        code, out, _err = self.run_main(PLAYER)
        self.assertEqual(0, code)
        self.assertIn("уже", out)

    def test_missing_config_fails(self) -> None:
        code, _out, err = self.run_main(PLAYER)
        self.assertEqual(1, code)
        self.assertIn("установщик claude-tts", err)
        self.assertFalse(self.path.exists())

    def test_relative_player_is_refused(self) -> None:
        self.assertEqual(2, self.run_main("tts-player")[0])


if __name__ == "__main__":
    unittest.main()
