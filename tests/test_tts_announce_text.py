"""Your own text for the half-hourly update: the setting, the placeholders, the command, the clip."""
import asyncio, sys, os, unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import commands, guild_settings, tts, tts_commands
from track_queue import Track
from test_tts_commands import _Env, _Base, CLIP

EXAMPLE = "Okupantu radio, precīzs laiks ir @laiks un rindā ir @queue dziesmas"
NOW = datetime(2026, 11, 25, 14, 34, tzinfo=ZoneInfo("Europe/Riga"))


class TestSetting(_Base):
    def test_round_trip_clear_and_per_guild(self):
        self.assertIsNone(guild_settings.get_announce_text("1"))
        guild_settings.set_announce_text("1", "hello")
        self.assertEqual(guild_settings.get_announce_text("1"), "hello")
        self.assertIsNone(guild_settings.get_announce_text("2"))
        guild_settings.set_announce_text("1", None)
        self.assertIsNone(guild_settings.get_announce_text("1"))
        guild_settings.set_announce_text("1", "x")
        guild_settings.set_announce_text("1", "")
        self.assertIsNone(guild_settings.get_announce_text("1"))


class TestFill(unittest.TestCase):
    def test_owner_example(self):
        self.assertEqual(tts.fill_announcement(EXAMPLE, NOW, 6),
                         "Okupantu radio, precīzs laiks ir 14:34 un rindā ir 6 dziesmas")

    def test_case_insensitive(self):
        self.assertEqual(tts.fill_announcement("@LAIKS @Queue", NOW, 3), "14:34 3")

    def test_every_occurrence(self):
        self.assertEqual(tts.fill_announcement("@laiks, @laiks", NOW, 1), "14:34, 14:34")

    def test_whole_words_only(self):
        self.assertEqual(tts.fill_announcement("@queuex @laiks", NOW, 1), "@queuex 14:34")

    def test_no_placeholders(self):
        self.assertEqual(tts.fill_announcement("just words", NOW, 1), "just words")

    def test_zero_padded(self):
        self.assertEqual(tts.fill_announcement("@laiks", datetime(2026, 1, 1, 9, 5), 0), "09:05")


class TestClip(_Base):
    NOWTS = NOW.timestamp()

    def test_custom_text_is_filled_then_cleaned(self):
        guild_settings.set_announce("1", True)
        guild_settings.set_announce_text("1", EXAMPLE)
        env = _Env()
        for n in ("a", "b"):
            env.gs.queue.add(Track(query=n, title=n, requested_by="u"))
        with patch.object(tts_commands.time, "time", return_value=self.NOWTS):
            out = asyncio.run(tts_commands._update_clip(env.bot, 1, self.NOWTS - 600, True))
        self.assertEqual(out, CLIP)
        filled = "Okupantu radio, precīzs laiks ir 14:34 un rindā ir 3 dziesmas"
        tts.synthesize.assert_awaited_once_with(tts.clean_text(filled, "lv"), "lv", "ffmpeg")

    def test_text_that_cleans_to_nothing_gives_none(self):
        guild_settings.set_announce("1", True)
        guild_settings.set_announce_text("1", "@@@")
        env = _Env()
        with patch.object(tts_commands.time, "time", return_value=self.NOWTS):
            out = asyncio.run(tts_commands._update_clip(env.bot, 1, self.NOWTS - 600, False))
        self.assertIsNone(out)
        tts.synthesize.assert_not_awaited()


class TestCommand(_Base):
    def _run(self, *args, **kw):
        env = _Env()
        with patch.object(tts_commands, "datetime") as dt:
            dt.now.return_value = NOW
            reply = env.run("announce", *args, **kw)
        return env, reply

    def test_set_text_replies_with_example(self):
        env = self._run("text", text=EXAMPLE)[0]
        self.assertEqual(guild_settings.get_announce_text("1"), EXAMPLE)
        self.assertIn('Update text set. It will sound like: "Okupantu radio, precīzs laiks ir 14:34 un rindā ir 0 dziesmas"',
                      " ".join(env.sent))

    def test_show_current_and_default(self):
        self.assertIn("built-in", self._run("text")[1])
        guild_settings.set_announce_text("1", "my words")
        self.assertIn("my words", self._run("text")[1])

    def test_reset(self):
        guild_settings.set_announce_text("1", "my words")
        _, reply = self._run("text", text="reset")
        self.assertIsNone(guild_settings.get_announce_text("1"))
        self.assertIn("Back to the built-in update text.", reply)

    def test_too_long_is_refused(self):
        _, reply = self._run("text", text="a" * 301)
        self.assertIn("That's too long — 300 characters max.", reply)
        self.assertIsNone(guild_settings.get_announce_text("1"))

    def test_empty_after_cleaning_is_refused_with_usage(self):
        _, reply = self._run("text", text="@@@")
        self.assertIn("announce text <text>", reply)
        self.assertIsNone(guild_settings.get_announce_text("1"))

    def test_on_off_still_work(self):
        self._run("on")
        self.assertTrue(guild_settings.get_announce("1"))
        self._run("off")
        self.assertFalse(guild_settings.get_announce("1"))

    def test_status_mentions_custom_text(self):
        self.assertIn("announce text", self._run()[1])
        guild_settings.set_announce_text("1", "x")
        self.assertIn("custom", self._run()[1].lower())

    def test_non_admin_is_refused(self):
        env = _Env()
        env.cog = tts_commands.TtsCog(env.bot)
        self.assertIn("requires bot admin", env.run("announce", "text", text="hi"))
        self.assertIsNone(guild_settings.get_announce_text("1"))


class TestHelp(unittest.TestCase):
    def _help(self, enabled):
        sent = []

        async def send(content=None, **kwargs):
            sent.append(content)

        bot = SimpleNamespace(command_prefix="!", tts_enabled=enabled)
        asyncio.run(commands.MusicCog.help_cmd.callback(SimpleNamespace(bot=bot), SimpleNamespace(send=send)))
        return "\n".join(sent)

    def test_help(self):
        self.assertIn("`!announce text <text>` — Your own words for that update", self._help(True))
        self.assertNotIn("announce", self._help(False))


if __name__ == "__main__":
    unittest.main()
