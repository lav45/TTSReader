"""Тесты sentence_count_worker: процесс-счётчик с поддельным пакетом claude_code_tts (как в
test_synth_worker: настоящий интерпретатор claude-tts тестам не нужен)."""
import tests  # noqa: F401 — окружение тестов (tests/__init__.py)
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WORKER = Path(__file__).resolve().parent.parent / "tts_player" / "sentence_count_worker.py"

# Как настоящий split_sentences: пустой текст — одно пустое предложение; счётчик повторяет демон,
# который пустой текст не синтезирует вовсе
FAKE_BRIDGE = '''
def split_sentences(text):
    parts = [p.strip() + "." for p in text.split(".") if p.strip()]
    return parts or [text.strip()]
'''


class SentenceCountWorkerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.pkg = root / "fake" / "claude_code_tts"
        self.pkg.mkdir(parents=True)
        (self.pkg / "__init__.py").write_text("")
        (self.pkg / "bridge.py").write_text(FAKE_BRIDGE)
        self.fake_path = str(root / "fake")

    def run_worker(self, requests: list[str], with_package: bool = True) -> tuple[int, list]:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        if with_package:
            env["PYTHONPATH"] = self.fake_path
        proc = subprocess.run([sys.executable, "-P", str(WORKER)], input="".join(requests),
                              capture_output=True, text=True, env=env, timeout=30)
        return proc.returncode, [json.loads(line) for line in proc.stdout.splitlines()]

    @staticmethod
    def request(text: str) -> str:
        return json.dumps({"text": text}, ensure_ascii=False) + "\n"

    def test_counts_each_request_in_order(self):
        code, lines = self.run_worker([self.request("Раз. Два. Три."), self.request("Один."),
                                       self.request("А.\nБ.")])
        self.assertEqual(0, code)
        self.assertEqual([{"count": 3, "rest_at": 5}, {"count": 1, "rest_at": None},
                          {"count": 2, "rest_at": 3}], lines)

    def test_rest_at_splits_text_into_first_sentence_and_rest(self):
        """text[:rest_at] делится на первое предложение, text[rest_at:] — на остальные."""
        text = "  Раз.\n\n Два. Три."
        _, [line] = self.run_worker([self.request(text)])
        self.assertEqual(9, line["rest_at"])
        self.assertEqual(("  Раз.\n\n ", "Два. Три."), (text[:9], text[9:]))

    def test_blank_text_is_zero(self):
        """Демон пустой текст не синтезирует: ни одного предложения."""
        code, lines = self.run_worker([self.request(""), self.request("  \n\t ")])
        self.assertEqual(0, code)
        self.assertEqual([{"count": 0, "rest_at": None}] * 2, lines)

    def test_answer_comes_before_next_request(self):
        """Постоянный процесс: ответ на запрос сразу, не дожидаясь конца stdin."""
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PYTHONPATH"] = self.fake_path
        proc = subprocess.Popen([sys.executable, "-P", str(WORKER)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True, env=env)
        try:
            proc.stdin.write(self.request("Раз. Два."))
            proc.stdin.flush()
            self.assertEqual(2, json.loads(proc.stdout.readline())["count"])
            proc.stdin.write(self.request("Раз."))
            proc.stdin.flush()
            self.assertEqual(1, json.loads(proc.stdout.readline())["count"])
            proc.stdin.close()
            self.assertEqual(0, proc.wait(timeout=30))
        finally:
            proc.kill()
            proc.wait()
            proc.stdout.close()

    def test_bad_request_gives_error_and_exit_1(self):
        for line in ("not json\n", "[]\n", '{"text": 5}\n'):
            with self.subTest(line=line):
                code, lines = self.run_worker([self.request("Раз."), line])
                self.assertEqual(1, code)
                self.assertEqual(1, lines[0]["count"])
                self.assertIn("error", lines[-1])

    def test_missing_package_gives_exit_2(self):
        code, lines = self.run_worker([self.request("Раз.")], with_package=False)
        self.assertEqual(2, code)
        self.assertEqual(1, len(lines))
        self.assertIn("error", lines[0])

    def test_import_crash_gives_exit_2(self):
        (self.pkg / "bridge.py").write_text("raise RuntimeError('x')")
        code, lines = self.run_worker([self.request("Раз.")])
        self.assertEqual(2, code)
        self.assertIn("error", lines[0])


if __name__ == "__main__":
    unittest.main()
