"""Тесты PlayerSettings: файл настроек плеера (tail, autoplay)."""
import tests  # noqa: F401 — окружение тестов (tests/__init__.py)
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tts_player.settings_error import SettingsError
from tts_player.player_settings import PlayerSettings, Prefs

LEGACY_KEY = "duck"  # ключ удалённой настройки приглушения


class PlayerSettingsCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "tts-player"
        self.path = self.dir / "settings.json"
        self.settings = PlayerSettings(self.path)

    def write_raw(self, text):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")

    def load_warned(self, text):
        self.write_raw(text)
        with self.assertLogs("tts_player.player_settings", "WARNING"):
            return self.settings.load()


class DefaultPathTest(unittest.TestCase):
    def test_xdg_config_home(self):
        path = PlayerSettings.default_path({"XDG_CONFIG_HOME": "/x/cfg", "HOME": "/h"})
        self.assertEqual(path, Path("/x/cfg/tts-player/settings.json"))

    def test_without_xdg(self):
        path = PlayerSettings.default_path({"HOME": "/h"})
        self.assertEqual(path, Path("/h/.config/tts-player/settings.json"))

    def test_empty_xdg_ignored(self):
        path = PlayerSettings.default_path({"XDG_CONFIG_HOME": "", "HOME": "/h"})
        self.assertEqual(path, Path("/h/.config/tts-player/settings.json"))


class LoadTest(PlayerSettingsCase):
    def test_no_file(self):
        self.assertEqual(self.settings.load(), Prefs(5))

    def test_valid(self):
        self.write_raw('{"tail": 7}')
        self.assertEqual(self.settings.load(), Prefs(7))

    def test_missing_autoplay_defaults_to_true(self):
        self.write_raw('{"tail": 7}')
        self.assertIs(self.settings.load().autoplay, True)

    def test_autoplay_false_read_without_warning(self):
        self.write_raw('{"tail": 7, "autoplay": false}')
        with self.assertNoLogs("tts_player.player_settings", "WARNING"):
            self.assertEqual(self.settings.load(), Prefs(7, False))

    def test_legacy_key_ignored_without_warning(self):
        """Ключ удалённой настройки остался в файлах прежних версий: не читается и не предупреждает."""
        self.write_raw('{"tail": 7, "%s": 55, "autoplay": false}' % LEGACY_KEY)
        with self.assertNoLogs("tts_player.player_settings", "WARNING"):
            self.assertEqual(self.settings.load(), Prefs(tail=7, autoplay=False))

    def test_bad_legacy_value_ignored_without_warning(self):
        self.write_raw('{"tail": 7, "%s": "loud"}' % LEGACY_KEY)
        with self.assertNoLogs("tts_player.player_settings", "WARNING"):
            self.assertEqual(self.settings.load(), Prefs(tail=7))

    def test_bad_autoplay_defaults_to_true_keeps_others(self):
        for autoplay in ("0", "1", '"no"', "null", "2.5"):
            with self.subTest(autoplay=autoplay):
                prefs = self.load_warned('{"tail": 7, "autoplay": %s}' % autoplay)
                self.assertEqual(prefs, Prefs(7, True))

    def test_broken_json(self):
        self.assertEqual(self.load_warned("{oops"), Prefs())

    def test_not_object(self):
        self.assertEqual(self.load_warned("[1, 2]"), Prefs())

    def test_bad_tail_keeps_autoplay(self):
        for tail in ("0", "21", '"5"', "true", "2.5", "null"):
            with self.subTest(tail=tail):
                prefs = self.load_warned('{"tail": %s, "autoplay": false}' % tail)
                self.assertEqual(prefs, Prefs(5, False))

    def test_bounds_accepted(self):
        for tail in (1, 20):
            with self.subTest(tail=tail):
                self.write_raw(json.dumps({"tail": tail}))
                self.assertEqual(self.settings.load().tail, tail)


class SaveTest(PlayerSettingsCase):
    def test_roundtrip_creates_dir_without_tmp(self):
        self.settings.save(Prefs(3))
        self.assertEqual(self.settings.load(), Prefs(3))
        self.assertEqual(json.loads(self.path.read_text()), {"tail": 3, "autoplay": True})
        self.assertEqual([p.name for p in self.dir.iterdir()], ["settings.json"])

    def test_autoplay_false_is_written(self):
        self.settings.save(Prefs(autoplay=False))
        self.assertEqual(json.loads(self.path.read_text()),
                         {"tail": 5, "autoplay": False})

    def test_bad_tail_leaves_file_untouched(self):
        self.write_raw('{"tail": 4}')
        before = self.path.read_bytes()
        for tail in (0, 21, "5", True, 2.5):
            with self.subTest(tail=tail), self.assertRaises(SettingsError):
                self.settings.save(Prefs(tail))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["settings.json"])

    def test_no_write_permission(self):
        self.dir.mkdir(parents=True)
        self.dir.chmod(0o555)
        self.addCleanup(self.dir.chmod, 0o755)
        if os.access(self.dir, os.W_OK):
            self.skipTest("права не действуют (root)")
        with self.assertRaises(SettingsError):
            self.settings.save(Prefs(3))

    def test_mkdir_failure(self):
        blocker = Path(self.dir.parent / "file")
        blocker.write_text("x")
        with self.assertRaises(SettingsError):
            PlayerSettings(blocker / "sub" / "settings.json").save(Prefs(3))

    def test_non_oserror_leaves_no_tmp(self):
        self.dir.mkdir(parents=True)
        with mock.patch("tts_player.atomic_json.json.dump", side_effect=TypeError("boom")):
            with self.assertRaises(TypeError):
                self.settings.save(Prefs(3))
        self.assertEqual(list(self.dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
