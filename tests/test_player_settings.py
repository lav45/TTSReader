"""Тесты PlayerSettings: файл настроек плеера (tail, autoplay, duck)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tts_player.settings_error import SettingsError
from tts_player.player_settings import PlayerSettings, Prefs


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
        self.assertEqual(self.settings.load(), Prefs(5, True, 30))

    def test_valid(self):
        self.write_raw('{"tail": 7, "autoplay": false}')
        self.assertEqual(self.settings.load(), Prefs(7, False))

    def test_broken_json(self):
        self.assertEqual(self.load_warned("{oops"), Prefs(5, True))

    def test_not_object(self):
        self.assertEqual(self.load_warned("[1, 2]"), Prefs(5, True))

    def test_bad_tail_keeps_autoplay(self):
        for tail in ("0", "21", '"5"', "true", "2.5", "null"):
            with self.subTest(tail=tail):
                prefs = self.load_warned('{"tail": %s, "autoplay": false}' % tail)
                self.assertEqual(prefs, Prefs(5, False))

    def test_bad_autoplay_keeps_tail(self):
        prefs = self.load_warned('{"tail": 3, "autoplay": "no"}')
        self.assertEqual(prefs, Prefs(3, True))

    def test_duck_read(self):
        self.write_raw('{"tail": 7, "autoplay": false, "duck": 55}')
        self.assertEqual(self.settings.load(), Prefs(7, False, 55))

    def test_duck_bounds_accepted(self):
        for duck in (0, 100):
            with self.subTest(duck=duck):
                self.write_raw(json.dumps({"tail": 5, "autoplay": True, "duck": duck}))
                self.assertEqual(self.settings.load().duck, duck)

    def test_bad_duck_keeps_others(self):
        for duck in ("101", "-1", '"30"', "true", "2.5", "null"):
            with self.subTest(duck=duck):
                prefs = self.load_warned('{"tail": 3, "autoplay": false, "duck": %s}' % duck)
                self.assertEqual(prefs, Prefs(3, False, 30))

    def test_bounds_accepted(self):
        for tail in (1, 20):
            with self.subTest(tail=tail):
                self.write_raw(json.dumps({"tail": tail, "autoplay": True}))
                self.assertEqual(self.settings.load().tail, tail)


class SaveTest(PlayerSettingsCase):
    def test_roundtrip_creates_dir_without_tmp(self):
        self.settings.save(Prefs(3, False, 40))
        self.assertEqual(self.settings.load(), Prefs(3, False, 40))
        self.assertEqual(json.loads(self.path.read_text()),
                         {"tail": 3, "autoplay": False, "duck": 40})
        self.assertEqual([p.name for p in self.dir.iterdir()], ["settings.json"])

    def test_bad_tail_leaves_file_untouched(self):
        self.write_raw('{"tail": 4, "autoplay": true}')
        before = self.path.read_bytes()
        for tail in (0, 21, "5", True, 2.5):
            with self.subTest(tail=tail), self.assertRaises(SettingsError):
                self.settings.save(Prefs(tail, True))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["settings.json"])

    def test_bad_duck_leaves_file_untouched(self):
        self.write_raw('{"tail": 4, "autoplay": true, "duck": 30}')
        before = self.path.read_bytes()
        for duck in (-1, 101, "30", True, 2.5):
            with self.subTest(duck=duck), self.assertRaises(SettingsError):
                self.settings.save(Prefs(4, True, duck))
        self.assertEqual(self.path.read_bytes(), before)

    def test_no_write_permission(self):
        self.dir.mkdir(parents=True)
        self.dir.chmod(0o555)
        self.addCleanup(self.dir.chmod, 0o755)
        if os.access(self.dir, os.W_OK):
            self.skipTest("права не действуют (root)")
        with self.assertRaises(SettingsError):
            self.settings.save(Prefs(3, True))

    def test_mkdir_failure(self):
        blocker = Path(self.dir.parent / "file")
        blocker.write_text("x")
        with self.assertRaises(SettingsError):
            PlayerSettings(blocker / "sub" / "settings.json").save(Prefs(3, True))

    def test_non_oserror_leaves_no_tmp(self):
        self.dir.mkdir(parents=True)
        with mock.patch("tts_player.atomic_json.json.dump", side_effect=TypeError("boom")):
            with self.assertRaises(TypeError):
                self.settings.save(Prefs(3, True))
        self.assertEqual(list(self.dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
