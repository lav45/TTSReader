"""Окружение тестов (tests/__init__.py): GIO без GVfs при любом способе запуска."""
import tests  # noqa: F401 — окружение тестов (tests/__init__.py)
import os
import ast
import unittest
from pathlib import Path

LINE = "import tests  # noqa: F401 — окружение тестов (tests/__init__.py)"
TESTS = Path(__file__).resolve().parent


class TestsEnvTest(unittest.TestCase):
    def test_gvfs_is_off(self) -> None:
        self.assertEqual("local", os.environ.get("GIO_USE_VFS"))

    def test_every_test_module_imports_env_first(self) -> None:
        """Строка окружения — первый импорт каждого модуля тестов: иначе запуск этого модуля
        отдельно (-p, tests.X) идёт с GVfs на шине пользователя."""
        modules = [*TESTS.glob("test_*.py"), TESTS / "gui_window_cases.py"]
        for path in modules:
            source = path.read_text()
            imports = [node for node in ast.parse(source).body
                       if isinstance(node, (ast.Import, ast.ImportFrom))]
            with self.subTest(module=path.name):
                self.assertTrue(imports, f"{path.name}: в модуле нет ни одного импорта")
                first_line = source.splitlines()[imports[0].lineno - 1]
                self.assertEqual(LINE, first_line)


if __name__ == "__main__":
    unittest.main()
