"""Per-server TTS settings: the language, the !tts switch and the half-hourly update."""
import os, sys, tempfile, unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import guild_settings as gsettings


class TestTtsSettings(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = patch.object(gsettings, "SETTINGS_FILE", os.path.join(tmp.name, "gs.json"))
        p.start(); self.addCleanup(p.stop)

    def test_defaults(self):
        self.assertEqual(gsettings.get_tts_language("1"), "lv")
        self.assertTrue(gsettings.get_tts_allowed("1"))
        self.assertFalse(gsettings.get_announce("1"))

    def test_round_trips_per_guild(self):
        gsettings.set_tts_language("1", "en")
        gsettings.set_tts_allowed("1", False)
        gsettings.set_announce("1", True)
        self.assertEqual((gsettings.get_tts_language("1"), gsettings.get_tts_allowed("1"),
                          gsettings.get_announce("1")), ("en", False, True))
        self.assertEqual((gsettings.get_tts_language("2"), gsettings.get_tts_allowed("2"),
                          gsettings.get_announce("2")), ("lv", True, False))

    def test_unknown_language_is_refused(self):
        with self.assertRaises(ValueError):
            gsettings.set_tts_language("1", "de")
        self.assertEqual(gsettings.get_tts_language("1"), "lv")

    def test_other_keys_survive(self):
        gsettings.set_timezone("1", "Europe/Berlin")
        gsettings.set_announce("1", True)
        self.assertEqual(gsettings.get_timezone("1"), "Europe/Berlin")
