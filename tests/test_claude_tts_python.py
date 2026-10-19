"""Интерпретатор claude-tts — из скрипта claude-tts в PATH, обе формы скрипта uv."""
import tests  # noqa: F401 — окружение тестов (tests/__init__.py)
import tempfile
import unittest
from pathlib import Path

from tts_player.claude_tts_python import claude_tts_python


class ClaudeTtsPythonTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.script = Path(tmp.name) / "claude-tts"

    def resolve(self, text: str) -> Path | None:
        self.script.write_text(text)
        return claude_tts_python(lambda name: str(self.script))

    def test_direct_shebang(self) -> None:
        self.assertEqual(Path("/u/tools/claude-code-tts/bin/python"),
                         self.resolve("#!/u/tools/claude-code-tts/bin/python\nimport sys\n"))

    def test_sh_exec_form_for_long_or_spaced_paths(self) -> None:
        text = "#!/bin/sh\n'''exec' '/Users/a b/tools/bin/python' \"$0\" \"$@\"\n' '''\n"
        self.assertEqual(Path("/Users/a b/tools/bin/python"), self.resolve(text))

    def test_env_shebang_names_no_environment(self) -> None:
        self.assertIsNone(self.resolve("#!/usr/bin/env python3\n"))

    def test_missing_claude_tts(self) -> None:
        self.assertIsNone(claude_tts_python(lambda name: None))


if __name__ == "__main__":
    unittest.main()
