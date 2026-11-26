"""Text-to-speech is opt-in: tts.enabled in config.yaml plus the edge-tts package."""
import asyncio, os, subprocess, sys, unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tts

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(REPO, name), encoding="utf-8") as f:
        return f.read()


class TestWanted(unittest.TestCase):
    def test_off_unless_enabled(self):
        for config in ({}, {"tts": None}, {"tts": {}}, {"tts": {"enabled": False}}):
            self.assertFalse(tts.wanted(config), config)
        self.assertTrue(tts.wanted({"tts": {"enabled": True}}))

    def test_a_malformed_setting_leaves_it_off(self):
        # `tts: true` is a plausible slip, and a quoted "false" reads as truthy.
        for config in ({"tts": True}, {"tts": "on"}, {"tts": ["enabled"]},
                       {"tts": {"enabled": "false"}}, {"tts": {"enabled": "true"}}):
            self.assertFalse(tts.wanted(config), config)


class TestStartupStatus(unittest.TestCase):
    def test_not_wanted(self):
        with patch.object(tts, "installed", return_value=True):
            self.assertEqual(tts.startup_status({}),
                             (False, "Text-to-speech is off (set tts.enabled: true in config.yaml to turn it on)."))

    def test_wanted_but_not_installed(self):
        with patch.object(tts, "installed", return_value=False):
            enabled, line = tts.startup_status({"tts": {"enabled": True}})
        self.assertFalse(enabled)
        self.assertIn("pip install -r requirements-tts.txt", line)
        self.assertIn("WITH_TTS=true", line)

    def test_on(self):
        with patch.object(tts, "installed", return_value=True):
            self.assertEqual(tts.startup_status({"tts": {"enabled": True}}), (True, "Text-to-speech is on."))

    def test_installed_follows_find_spec(self):
        with patch("importlib.util.find_spec", return_value=None):
            self.assertFalse(tts.installed())
        with patch("importlib.util.find_spec", return_value=object()):
            self.assertTrue(tts.installed())


class TestWithoutEdgeTts(unittest.TestCase):
    def test_tts_imports_without_the_package(self):
        code = "import sys; sys.modules['edge_tts'] = None; import tts; print('ok')"
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "ok", out.stderr)

    def test_synthesis_without_the_package_is_a_tts_error(self):
        with patch.dict(sys.modules, {"edge_tts": None}):
            with self.assertRaises(tts.TtsError):
                asyncio.run(tts.synthesize("hi", "en", "ffmpeg"))


class TestMusicBotFlag(unittest.TestCase):
    def test_bot_records_the_status(self):
        import main
        with patch.object(main.tts, "startup_status", return_value=(True, "Text-to-speech is on.")):
            bot = main.MusicBot({"prefix": "!"})
        self.assertTrue(bot.tts_enabled)
        self.assertEqual(bot.tts_status, "Text-to-speech is on.")

    def test_a_malformed_setting_does_not_stop_the_bot(self):
        # The status is read in MusicBot.__init__: raising there took the music down too.
        import main
        bot = main.MusicBot({"prefix": "!", "tts": True})
        self.assertFalse(bot.tts_enabled)


class TestPackaging(unittest.TestCase):
    def test_edge_tts_is_optional(self):
        self.assertNotIn("edge-tts", _read("requirements.txt"))
        self.assertIn("edge-tts>=7.3.1", _read("requirements-tts.txt"))

    def test_docker_installs_it_only_on_request(self):
        dockerfile = _read("Dockerfile")
        self.assertIn("ARG WITH_TTS=false", dockerfile)
        self.assertIn("requirements-tts.txt", dockerfile)
        self.assertIn("WITH_TTS", _read("docker-compose.yml"))

    def test_example_config_ships_it_off(self):
        import yaml
        self.assertIs(yaml.safe_load(_read("config.example.yaml"))["tts"]["enabled"], False)
