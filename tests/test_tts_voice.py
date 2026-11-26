"""A male or female voice per server: the setting, the voice choice, where it is passed, the command."""
import asyncio, sys, os, unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import commands, guild_settings, tts, tts_commands
from track_queue import Track
from test_tts_commands import _Env, _Base, CLIP


class TestSetting(_Base):
    def test_default_is_female(self):
        self.assertEqual(guild_settings.get_tts_voice("1"), "female")

    def test_round_trip_and_per_guild(self):
        guild_settings.set_tts_voice("1", "male")
        self.assertEqual(guild_settings.get_tts_voice("1"), "male")
        self.assertEqual(guild_settings.get_tts_voice("2"), "female")

    def test_bad_value_raises_and_changes_nothing(self):
        guild_settings.set_tts_voice("1", "male")
        with self.assertRaises(ValueError):
            guild_settings.set_tts_voice("1", "robot")
        self.assertEqual(guild_settings.get_tts_voice("1"), "male")

    def test_junk_in_the_file_reads_as_default(self):
        with guild_settings._guild_entry("1") as g:
            g["tts_voice"] = "robot"
        self.assertEqual(guild_settings.get_tts_voice("1"), "female")


class TestSynthesizeVoice(unittest.TestCase):
    def _voice(self, lang, **kw):
        fetch = AsyncMock(return_value=b"mp3")
        with patch.object(tts, "_fetch_mp3", fetch), \
             patch.object(tts, "_decode_to_pcm", return_value=b"\x01" * 10):
            asyncio.run(tts.synthesize("x", lang, "ffmpeg", **kw))
        return fetch.await_args.args[1]

    def test_male_latvian(self):
        self.assertEqual(self._voice("lv", voice="male"), "lv-LV-NilsNeural")

    def test_male_english(self):
        self.assertEqual(self._voice("en", voice="male"), "en-US-AndrewNeural")

    def test_default_is_female(self):
        self.assertEqual(self._voice("lv"), "lv-LV-EveritaNeural")
        self.assertEqual(self._voice("en", voice="female"), "en-US-AriaNeural")


class TestPassed(_Base):
    def test_tts_flow_passes_the_voice(self):
        guild_settings.set_tts_voice("1", "male")
        _Env().run("tts", text="hei")
        tts.synthesize.assert_awaited_once_with("hei", "lv", "ffmpeg", voice="male")

    def test_update_clip_passes_the_voice(self):
        guild_settings.set_tts_voice("1", "male")
        guild_settings.set_announce("1", True)
        now = datetime(2026, 11, 25, 14, 34, tzinfo=ZoneInfo("Europe/Riga")).timestamp()
        env = _Env()
        with patch.object(tts_commands.time, "time", return_value=now):
            asyncio.run(tts_commands._update_clip(env.bot, 1, now - 600, False))
        self.assertEqual(tts.synthesize.await_args.kwargs, {"voice": "male"})


class TestCommand(_Base):
    def test_sets_the_voice(self):
        self.assertIn("Speech voice set to **male**.", _Env().run("ttsvoice", "male"))
        self.assertEqual(guild_settings.get_tts_voice("1"), "male")

    def test_no_argument_shows_the_current_voice(self):
        reply = _Env().run("ttsvoice")
        self.assertIn("**female**", reply)
        self.assertIn("ttsvoice female|male", reply)

    def test_bad_value_is_refused(self):
        self.assertIn("Use `female` or `male`.", _Env().run("ttsvoice", "robot"))
        self.assertEqual(guild_settings.get_tts_voice("1"), "female")

    def test_non_admin_is_refused(self):
        env = _Env()
        env.cog = tts_commands.TtsCog(env.bot)
        self.assertIn("requires bot admin", env.run("ttsvoice", "male"))
        self.assertEqual(guild_settings.get_tts_voice("1"), "female")


class TestHelp(unittest.TestCase):
    def _help(self, enabled):
        sent = []

        async def send(content=None, **kwargs):
            sent.append(content)

        bot = SimpleNamespace(command_prefix="!", tts_enabled=enabled)
        asyncio.run(commands.MusicCog.help_cmd.callback(SimpleNamespace(bot=bot), SimpleNamespace(send=send)))
        return "\n".join(sent)

    def test_listed_only_with_tts_on(self):
        self.assertIn("`!ttsvoice female|male` — Speech voice for this server *(admin only)*", self._help(True))
        self.assertNotIn("ttsvoice", self._help(False))


if __name__ == "__main__":
    unittest.main()
