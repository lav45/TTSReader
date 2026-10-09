"""Тесты DaemonSettings: голоса, активная персона и скорость из настроек claude-tts."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tts_player.daemon_settings import ActiveVoice, DaemonSettings, Voice
from tts_player.settings_error import SettingsError

NAMES = [
    "claude-prime", "claude-chill", "code-reviewer",
    "ru-denis", "ru-irina", "ru-ruslan", "supertonic-3",
]


def make_config() -> dict:
    """Литерал по форме настоящего ~/.claude-tts/config.json."""
    personas = {}
    for name in NAMES:
        personas[name] = {
            "description": f"Голос {name}",
            "voice": "af_heart",
            "speed": 1.8 if name == "supertonic-3" else 1.0,
            "speed_method": "tempo",
            "voice_sherpa": "ru_RU-denis",
            "speaker_sherpa": 0,
        }
    del personas["code-reviewer"]["description"]
    return {
        "version": 2,
        "mode": "queue",
        "active_persona": "supertonic-3",
        "personas": personas,
        "project_personas": {"sess-1": "ru-irina"},
        "queue": {"max": 5},
        "sounds": {"enabled": True},
    }


class DaemonSettingsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = self.root / "config.json"
        self.sessions = self.root / "sessions.d"
        self.sessions.mkdir()
        self.write_config(make_config())
        self.settings = DaemonSettings(self.root)

    def write_config(self, data: dict) -> None:
        self.config.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def read_config(self) -> dict:
        return json.loads(self.config.read_text(encoding="utf-8"))

    def test_load_real_format(self):
        result = self.settings.load()
        self.assertEqual([v.name for v in result.voices], NAMES)
        self.assertEqual(result.voices[0], Voice("claude-prime", "Голос claude-prime"))
        self.assertEqual(result.voices[2].description, "")
        self.assertEqual(result.active, "supertonic-3")
        self.assertEqual(result.speed, 1.8)

    def test_load_missing_file(self):
        self.config.unlink()
        self.assert_load_error()

    def test_load_broken_json(self):
        self.config.write_text("{oops", encoding="utf-8")
        self.assert_load_error()

    def test_load_without_personas(self):
        data = make_config()
        del data["personas"]
        self.write_config(data)
        self.assert_load_error()

    def test_load_empty_personas(self):
        data = make_config()
        data["personas"] = {}
        self.write_config(data)
        self.assert_load_error()

    def test_load_active_not_in_personas(self):
        data = make_config()
        data["active_persona"] = "nobody"
        self.write_config(data)
        self.assert_load_error()

    def test_load_speed_out_of_range(self):
        for speed in (0.4, 4.1):
            data = make_config()
            data["personas"]["supertonic-3"]["speed"] = speed
            self.write_config(data)
            with self.assertRaises(SettingsError) as ctx:
                self.settings.load()
            self.assertIn("supertonic-3", str(ctx.exception))
            self.assertIn(str(speed), str(ctx.exception))

    def test_load_boundary_speeds(self):
        for speed in (0.5, 4.0):
            data = make_config()
            data["personas"]["supertonic-3"]["speed"] = speed
            self.write_config(data)
            self.assertEqual(speed, self.settings.load().speed)

    def assert_load_error(self):
        with self.assertRaises(SettingsError) as ctx:
            self.settings.load()
        self.assertTrue(str(ctx.exception))

    def test_save_config(self):
        before = make_config()
        self.settings.save("ru-denis", 1.3)
        after = self.read_config()
        self.assertEqual(after["active_persona"], "ru-denis")
        self.assertNotIn("project_personas", after)
        for name, persona in after["personas"].items():
            self.assertEqual(persona["speed"], 1.3)
            expected = dict(before["personas"][name], speed=1.3)
            self.assertEqual(persona, expected)
        for key in ("version", "mode", "queue", "sounds"):
            self.assertEqual(after[key], before[key])

    def test_save_rereads_config(self):
        self.settings.load()
        data = make_config()
        data["added_later"] = {"x": 1}
        self.write_config(data)
        self.settings.save("ru-irina", 1.0)
        self.assertEqual(self.read_config()["added_later"], {"x": 1})

    def test_save_cleans_sessions(self):
        other = {"persona": "ru-irina", "speed": 2.0, "tone": "calm", "name": "Привет"}
        (self.sessions / "a.json").write_text(json.dumps(other), encoding="utf-8")
        self.settings.save("ru-denis", 1.3)
        got = json.loads((self.sessions / "a.json").read_text(encoding="utf-8"))
        self.assertEqual(got, {"tone": "calm", "name": "Привет"})

    def test_save_untouched_session_keeps_mtime(self):
        path = self.sessions / "b.json"
        path.write_text(json.dumps({"tone": "calm"}), encoding="utf-8")
        os.utime(path, (1000, 1000))
        before = path.read_bytes()
        self.settings.save("ru-denis", 1.3)
        self.assertEqual(path.stat().st_mtime, 1000)
        self.assertEqual(path.read_bytes(), before)

    def test_save_skips_invalid_sessions(self):
        broken = self.sessions / "broken.json"
        broken.write_text("{oops", encoding="utf-8")
        listed = self.sessions / "list.json"
        listed.write_text("[1, 2]", encoding="utf-8")
        self.settings.save("ru-denis", 1.3)
        self.assertEqual(broken.read_text(encoding="utf-8"), "{oops")
        self.assertEqual(listed.read_text(encoding="utf-8"), "[1, 2]")
        self.assertEqual(self.read_config()["active_persona"], "ru-denis")

    def test_save_without_sessions_dir(self):
        self.sessions.rmdir()
        self.settings.save("ru-denis", 1.3)
        self.assertEqual(self.read_config()["active_persona"], "ru-denis")

    def test_save_rejects_bad_input_without_changes(self):
        (self.sessions / "a.json").write_text('{"persona": "x"}', encoding="utf-8")
        config_before = self.config.read_bytes()
        session_before = (self.sessions / "a.json").read_bytes()
        for persona, speed in (("nobody", 1.0), ("ru-denis", 0.4), ("ru-denis", 4.1)):
            with self.subTest(persona=persona, speed=speed):
                with self.assertRaises(SettingsError) as ctx:
                    self.settings.save(persona, speed)
                self.assertTrue(str(ctx.exception))
                self.assertEqual(self.config.read_bytes(), config_before)
                self.assertEqual((self.sessions / "a.json").read_bytes(), session_before)

    def test_save_boundary_speeds(self):
        self.settings.save("ru-denis", 0.5)
        self.settings.save("ru-denis", 4.0)
        self.assertEqual(self.read_config()["personas"]["ru-denis"]["speed"], 4.0)

    def test_save_leaves_no_temp_files(self):
        (self.sessions / "a.json").write_text('{"persona": "x"}', encoding="utf-8")
        self.settings.save("ru-denis", 1.3)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["config.json", "sessions.d"])
        self.assertEqual([p.name for p in self.sessions.iterdir()], ["a.json"])

    @unittest.skipIf(os.geteuid() == 0, "root игнорирует запрет записи")
    def test_save_readonly_dir(self):
        before = self.config.read_bytes()
        self.root.chmod(0o500)
        self.addCleanup(self.root.chmod, stat.S_IRWXU)
        with self.assertRaises(SettingsError) as ctx:
            self.settings.save("ru-denis", 1.3)
        self.assertTrue(str(ctx.exception))
        self.assertEqual(self.config.read_bytes(), before)

    def test_load_active_not_a_string(self):
        for active in (["a"], {"a": 1}, 5, None):
            with self.subTest(active=active):
                data = make_config()
                data["active_persona"] = active
                self.write_config(data)
                self.assert_load_error()

    def test_load_persona_without_speed_uses_default(self):
        data = make_config()
        del data["personas"]["supertonic-3"]["speed"]
        self.write_config(data)
        self.assertEqual(2.0, self.settings.load().speed)

    def test_load_non_numeric_speed_is_error(self):
        for speed in ("1.5", True, None, [1]):
            with self.subTest(speed=speed):
                data = make_config()
                data["personas"]["supertonic-3"]["speed"] = speed
                self.write_config(data)
                self.assert_load_error()

    def test_save_rejects_non_number_speed(self):
        before = self.config.read_bytes()
        for speed in (True, "1.5", None):
            with self.subTest(speed=speed):
                with self.assertRaises(SettingsError):
                    self.settings.save("ru-denis", speed)
                self.assertEqual(self.config.read_bytes(), before)

    def test_save_removes_temp_file_on_non_os_error(self):
        with mock.patch("tts_player.atomic_json.json.dump", side_effect=TypeError("boom")):
            with self.assertRaises(TypeError):
                self.settings.save("ru-denis", 1.3)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["config.json", "sessions.d"])

    def test_save_removes_own_leftover_temp_files(self):
        own = [self.root / "config.json.ab12.tmp", self.sessions / "a.json.cd34.tmp"]
        foreign = [self.root / "config.tmp", self.sessions / "sid-1.tmp"]
        for path in own + foreign:
            path.write_text("x", encoding="utf-8")
        self.settings.save("ru-denis", 1.3)
        for path in own:
            self.assertFalse(path.exists(), path)
        for path in foreign:
            self.assertTrue(path.exists(), path)

    def test_save_does_not_resurrect_session_deleted_meanwhile(self):
        path = self.sessions / "gone.json"
        path.write_text(json.dumps({"persona": "x", "tone": "calm"}), encoding="utf-8")
        real_mkstemp = tempfile.mkstemp

        def delete_then_mkstemp(*args, **kwargs):
            if kwargs.get("dir") == self.sessions:
                path.unlink()  # демон удалил сессию между нашим чтением и записью
            return real_mkstemp(*args, **kwargs)

        with mock.patch("tts_player.atomic_json.tempfile.mkstemp", delete_then_mkstemp):
            self.settings.save("ru-denis", 1.3)
        self.assertEqual([], list(self.sessions.iterdir()))
        self.assertEqual(self.read_config()["active_persona"], "ru-denis")

    @unittest.skipIf(os.geteuid() == 0, "root игнорирует запрет записи")
    def test_save_readonly_sessions_dir_keeps_written_config(self):
        path = self.sessions / "a.json"
        path.write_text(json.dumps({"speed": 2.0}), encoding="utf-8")
        self.sessions.chmod(0o500)
        self.addCleanup(self.sessions.chmod, stat.S_IRWXU)
        with self.assertRaises(SettingsError) as ctx:
            self.settings.save("ru-denis", 1.3)
        self.assertTrue(str(ctx.exception))
        self.assertEqual(self.read_config()["active_persona"], "ru-denis")  # без отката
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"speed": 2.0})


class ActiveVoiceTest(unittest.TestCase):
    """active_voice: текущая персона и поля задачи демона из неё, как их пишет хук."""

    setUp = DaemonSettingsTest.setUp
    write_config = DaemonSettingsTest.write_config

    def test_fields_of_active_persona_like_hook(self):
        voice = self.settings.active_voice()
        self.assertEqual(ActiveVoice("supertonic-3", 1.8, {
            "persona": "supertonic-3", "speed": 1.8, "speed_method": "tempo",
            "voice_kokoro": "", "voice_kokoro_blend": "", "voice_sherpa": "ru_RU-denis",
            "speaker_sherpa": 0, "voice_mlx": "", "speaker_mlx": "", "lang_mlx": "",
            "pitch_filter": ""}), voice)

    def test_missing_speed_and_method_take_defaults(self):
        config = make_config()
        del config["personas"]["supertonic-3"]["speed"]
        del config["personas"]["supertonic-3"]["speed_method"]
        self.write_config(config)
        voice = self.settings.active_voice()
        self.assertEqual((2.0, "playback"), (voice.speed, voice.fields["speed_method"]))

    def test_speed_out_of_screen_range_is_still_voice(self):
        """Диапазон спина экрана настроек к переозвучке не относится."""
        config = make_config()
        config["personas"]["supertonic-3"]["speed"] = 5.0
        self.write_config(config)
        self.assertEqual(5.0, self.settings.active_voice().speed)

    def test_broken_config_raises_settings_error(self):
        for config in ({"personas": {}}, {**make_config(), "active_persona": "nobody"}):
            self.write_config(config)
            with self.assertRaises(SettingsError):
                self.settings.active_voice()
        config = make_config()
        config["personas"]["supertonic-3"]["speed"] = "fast"
        self.write_config(config)
        with self.assertRaises(SettingsError):
            self.settings.active_voice()


if __name__ == "__main__":
    unittest.main()
