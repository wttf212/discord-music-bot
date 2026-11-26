"""The optional TTS cog: !tts / !ttsafter / admin commands, the update provider, the off switch."""
import asyncio, os, sys, tempfile, unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import commands, guild_settings, speech, tts, tts_commands
from track_queue import Track, TrackQueue

CLIP = b"\x00" * speech.FRAME_SIZE


class _Speech:
    def __init__(self, result="solo", waiting=0, error=None, starting=False, starting_radio=False):
        self.result, self.waiting, self.error, self.starting = result, waiting, error, starting
        self.starting_radio = starting_radio
        self.said, self.after, self.cuts = [], [], []
        self.between_songs = None

    async def say(self, pcm):
        if self.error:
            raise self.error
        self.said.append(pcm)
        return self.result

    def queue_after(self, pcm):
        self.after.append(pcm)

    def waiting_clips(self):
        return self.waiting

    def cut(self, drop_after_song=False):
        self.cuts.append(drop_after_song)


class _Env:
    """One guild; the author and the bot share voice channel 7 unless told otherwise."""

    def __init__(self, speech_=None, playing=False, radio=False, author_in_voice=True, bot_elsewhere=False):
        self.speech = speech_ or _Speech()
        self.channel = SimpleNamespace(id=7, connect=AsyncMock())
        self.vc = SimpleNamespace(channel=SimpleNamespace(id=8) if bot_elsewhere else self.channel,
                                  is_connected=lambda: True)
        self.player = SimpleNamespace(speech=self.speech, is_playing=playing, _ffmpeg_path="ffmpeg",
                                      _voice_client=self.vc)
        self.gs = SimpleNamespace(player=self.player, queue=TrackQueue())
        if radio or playing:
            self.gs.queue.current = Track(query="c", title="C", requested_by="u", is_radio=radio)
        self.sent = []
        self.bot = SimpleNamespace(command_prefix="!", config={}, get_guild_state=lambda gid: self.gs)
        self.message = SimpleNamespace(mentions=[], role_mentions=[], add_reaction=AsyncMock())
        self.ctx = SimpleNamespace(
            bot=self.bot, interaction=None, message=self.message, channel=SimpleNamespace(id=99),
            guild=SimpleNamespace(id=1, voice_client=self.vc),
            author=SimpleNamespace(id=42, voice=SimpleNamespace(channel=self.channel) if author_in_voice else None),
            send=AsyncMock(side_effect=self._send))
        self.cog = SimpleNamespace(bot=self.bot, _check_admin=AsyncMock(return_value=True))

    async def _send(self, content=None, **kwargs):
        self.sent.append(content)

    def run(self, name, *args, **kwargs):
        asyncio.run(getattr(tts_commands.TtsCog, name).callback(self.cog, self.ctx, *args, **kwargs))
        return " | ".join(m or "" for m in self.sent)


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (patch.object(guild_settings, "SETTINGS_FILE", os.path.join(tmp.name, "gs.json")),
                  patch.object(tts_commands, "check_channel", AsyncMock(return_value=True)),
                  patch.object(tts, "synthesize", AsyncMock(return_value=CLIP))):
            p.start(); self.addCleanup(p.stop)
        commands._button_cooldowns.clear()


class TestTts(_Base):
    def test_speaks_now_and_reacts(self):
        env = _Env()
        env.run("tts", text="labdien")
        self.assertEqual(env.speech.said, [CLIP])
        tts.synthesize.assert_awaited_once_with("labdien", "lv", "ffmpeg")
        env.message.add_reaction.assert_awaited_once_with("🔊")

    def test_each_accepted_clip_is_logged_with_its_author(self):
        # The author can delete a prefix !tts, and its reaction goes with it: the log keeps
        # who made the bot say what.
        for name, where in (("tts", "solo"), ("ttsafter", "after")):
            with self.subTest(command=name):
                commands._button_cooldowns.clear()
                env = _Env(playing=True) if name == "ttsafter" else _Env()
                with patch("builtins.print") as log:
                    env.run(name, text="labdien")
                lines = [" ".join(map(str, c.args)) for c in log.call_args_list]
                self.assertTrue(any(line.startswith("[tts] guild 1 user ") and "(42)" in line
                                    and line.endswith(f"-> {where}: labdien") for line in lines), lines)

    def test_after_song_while_a_song_plays_waits_for_the_change(self):
        env = _Env(playing=True)
        env.run("ttsafter", text="paldies")
        self.assertEqual((env.speech.after, env.speech.said), ([CLIP], []))

    def test_after_song_during_a_song_start_waits_for_the_next_change(self):
        env = _Env(speech_=_Speech(starting=True))     # the player isn't flagged as playing yet
        env.gs.queue.current = Track(query="c", title="C", requested_by="u")
        env.run("ttsafter", text="paldies")
        self.assertEqual((env.speech.after, env.speech.said), ([CLIP], []))

    def test_after_song_while_a_station_starts_speaks_with_it(self):
        # The song that ended is still queue.current until the station has started, but a
        # station never ends by itself: the clip goes with the station's start, like a !tts.
        env = _Env(speech_=_Speech(starting=True, starting_radio=True))
        env.gs.queue.current = Track(query="c", title="C", requested_by="u")
        env.run("ttsafter", text="paldies")
        self.assertEqual((env.speech.said, env.speech.after), ([CLIP], []))

    def test_after_song_during_radio_or_idle_speaks_now(self):
        for env in (_Env(playing=True, radio=True), _Env(playing=False)):
            commands._button_cooldowns.clear()          # both runs are guild 1 / user 42
            env.run("ttsafter", text="paldies")
            self.assertEqual((env.speech.said, env.speech.after), ([CLIP], []))

    def test_too_long_is_refused_before_synthesis(self):
        env = _Env()
        self.assertIn("300 characters", env.run("tts", text="x" * 301))
        tts.synthesize.assert_not_awaited()

    def test_cooldown(self):
        env = _Env()
        env.run("tts", text="viens")
        self.assertIn("one message every 10 seconds", env.run("tts", text="divi"))
        self.assertEqual(tts.synthesize.await_count, 1)

    def test_not_in_voice_is_refused(self):
        env = _Env(author_in_voice=False)
        self.assertIn("You need to be in a voice channel.", env.run("tts", text="hei"))
        tts.synthesize.assert_not_awaited()

    def test_other_channel_is_refused(self):
        env = _Env(bot_elsewhere=True)
        self.assertIn("same voice channel as the bot", env.run("tts", text="hei"))
        tts.synthesize.assert_not_awaited()

    def test_switched_off_server_is_refused(self):
        guild_settings.set_tts_allowed("1", False)
        env = _Env()
        self.assertIn("turned off on this server", env.run("tts", text="hei"))
        tts.synthesize.assert_not_awaited()

    def test_switched_off_during_synthesis_is_not_spoken(self):
        # !ttsallow off is the admin's brake: a clip still being made must not play after it.
        async def synthesize_meanwhile_switched_off(*args):
            guild_settings.set_tts_allowed("1", False)
            return CLIP
        tts.synthesize.side_effect = synthesize_meanwhile_switched_off
        env = _Env()
        self.assertIn("turned off on this server", env.run("tts", text="hei"))
        self.assertEqual((env.speech.said, env.speech.after), ([], []))

    def test_queue_filled_during_synthesis_is_refused(self):
        env = _Env()

        async def synthesize_while_others_queue(*args):
            env.speech.waiting = 5
            return CLIP
        tts.synthesize.side_effect = synthesize_while_others_queue
        self.assertIn("Too many messages waiting", env.run("tts", text="hei"))
        self.assertEqual(env.speech.said, [])

    def test_author_who_left_voice_during_synthesis_is_not_followed(self):
        env = _Env()
        env.ctx.guild.voice_client = None          # the bot would have to join the author
        env.player._voice_client = None

        async def synthesize_while_the_author_leaves(*args):
            env.ctx.author.voice = None
            return CLIP
        tts.synthesize.side_effect = synthesize_while_the_author_leaves
        self.assertIn("You need to be in a voice channel.", env.run("tts", text="hei"))
        env.channel.connect.assert_not_awaited()
        self.assertEqual(env.speech.said, [])

    def test_queue_full_is_refused(self):
        env = _Env(speech_=_Speech(waiting=5))
        self.assertIn("Too many messages waiting", env.run("tts", text="hei"))
        tts.synthesize.assert_not_awaited()

    def test_synthesis_failure_is_reported(self):
        tts.synthesize.side_effect = tts.TtsError("no audio")
        env = _Env()
        self.assertIn("Couldn't make that into speech", env.run("tts", text="😀😀😀"))
        self.assertEqual(env.speech.said, [])

    def test_voice_down_is_reported(self):
        env = _Env(speech_=_Speech(error=speech.SpeechUnavailable()))
        self.assertIn("Voice connection is down", env.run("tts", text="hei"))

    def test_paused_reply(self):
        env = _Env(speech_=_Speech(result="paused"))
        self.assertIn("Playback is paused", env.run("tts", text="hei"))

    def test_joins_the_authors_channel_when_not_connected(self):
        env = _Env()
        joined = SimpleNamespace(channel=env.channel, is_connected=lambda: True)
        env.ctx.guild.voice_client = None
        env.player._voice_client = None
        env.channel.connect = AsyncMock(return_value=joined)
        env.player.set_voice_client = MagicMock()
        env.player.set_bitrate = AsyncMock()
        env.player.set_eq = AsyncMock()
        env.run("tts", text="hei")
        env.player.set_voice_client.assert_called_once_with(joined)
        self.assertEqual(env.speech.said, [CLIP])

    def test_reconnecting_voice_is_left_alone(self):
        # A voice client that exists but isn't connected is discord.py reconnecting. Dropping it
        # to rejoin would stop the song; the clip goes to Speech, which reports the outage.
        env = _Env(speech_=_Speech(error=speech.SpeechUnavailable()))
        env.vc.is_connected = lambda: False
        env.vc.disconnect = AsyncMock()
        self.assertIn("Voice connection is down", env.run("tts", text="hei"))
        env.vc.disconnect.assert_not_awaited()
        env.channel.connect.assert_not_awaited()

    def test_channel_mentions_are_read_as_names(self):
        # The spec: user, role and channel mentions become names. discord.py's clean_content
        # leaves <#id> alone unless fix_channel_mentions is set, and the id would be read out.
        env = _Env()
        env.ctx.guild._resolve_channel = lambda channel_id: SimpleNamespace(name="general")
        env.run("tts", text="come to <#123456789012345678>")
        tts.synthesize.assert_awaited_once_with("come to #general", "lv", "ffmpeg")


class TestSlash(_Base):
    """/tts and /ttsafter: deferred before the synthesis, which can outlast Discord's 3 s, then a
    public reply with the text. That reply is what shows everyone who made the bot speak."""

    def _env(self, **kwargs):
        env = _Env(**kwargs)
        env.ctx.interaction = SimpleNamespace()
        env.ctx.defer = AsyncMock()
        env.deferred_first = []

        async def synthesize(*args):
            env.deferred_first.append(env.ctx.defer.await_count == 1)
            return CLIP
        tts.synthesize.side_effect = synthesize
        return env

    def test_deferred_then_a_public_reply_with_its_note(self):
        for name, env_kwargs, reply in (
                ("tts", {}, "🔊 hei"),
                ("ttsafter", {"playing": True}, "🔊 hei *(after this song)*"),
                ("tts", {"speech_": _Speech(result="paused")}, "🔊 hei *(when playback resumes)*")):
            with self.subTest(reply=reply):
                commands._button_cooldowns.clear()
                env = self._env(**env_kwargs)
                env.run(name, text="hei")
                self.assertEqual(env.deferred_first, [True], "synthesis began before the defer")
                self.assertEqual(env.sent, [reply])
                for call in env.ctx.send.await_args_list:
                    self.assertNotEqual(call.kwargs.get("ephemeral"), True, "the reply must be public")
                env.message.add_reaction.assert_not_awaited()


class TestAdmin(_Base):
    def test_ttsallow_off_cuts_speech(self):
        env = _Env()
        env.run("ttsallow", "off")
        self.assertFalse(guild_settings.get_tts_allowed("1"))
        self.assertEqual(env.speech.cuts, [True])

    def test_ttslang_validates(self):
        env = _Env()
        self.assertIn("Use `lv` or `en`.", env.run("ttslang", "de"))
        env.run("ttslang", "en")
        self.assertEqual(guild_settings.get_tts_language("1"), "en")

    def test_announce_on(self):
        _Env().run("announce", "on")
        self.assertTrue(guild_settings.get_announce("1"))


class TestUpdateClip(_Base):
    NOW = datetime(2026, 11, 25, 14, 34, tzinfo=ZoneInfo("Europe/Riga")).timestamp()

    def _call(self, env, started_at, starting_next=True):
        with patch.object(tts_commands.time, "time", return_value=self.NOW):
            return asyncio.run(tts_commands._update_clip(env.bot, 1, started_at, starting_next))

    def _env_with_two_queued(self):
        env = _Env()
        for name in ("a", "b"):
            env.gs.queue.add(Track(query=name, title=name, requested_by="u"))
        return env

    def test_off_by_default(self):
        self.assertIsNone(self._call(_Env(), self.NOW - 600))
        tts.synthesize.assert_not_awaited()

    def test_no_track_or_no_mark_no_update(self):
        guild_settings.set_announce("1", True)
        self.assertIsNone(self._call(_Env(), None))
        self.assertIsNone(self._call(_Env(), self.NOW - 60))      # started 14:33, no mark since

    def test_mark_passed_speaks_time_and_songs_left(self):
        guild_settings.set_announce("1", True)
        self.assertEqual(self._call(self._env_with_two_queued(), self.NOW - 600), CLIP)
        tts.synthesize.assert_awaited_once_with("Pulkstenis ir 14:34. Rindā vēl 3 dziesmas.", "lv", "ffmpeg")

    def test_a_station_starting_counts_only_the_queued_songs(self):
        # Speech passes starting_next=False at a radio start: the station is not a song.
        guild_settings.set_announce("1", True)
        self._call(self._env_with_two_queued(), self.NOW - 600, starting_next=False)
        tts.synthesize.assert_awaited_once_with("Pulkstenis ir 14:34. Rindā vēl 2 dziesmas.", "lv", "ffmpeg")

    def test_queue_end_says_it_is_empty(self):
        guild_settings.set_announce("1", True)
        self._call(_Env(), self.NOW - 600, starting_next=False)
        tts.synthesize.assert_awaited_once_with("Pulkstenis ir 14:34. Rinda ir tukša.", "lv", "ffmpeg")

    def test_english(self):
        guild_settings.set_announce("1", True)
        guild_settings.set_tts_language("1", "en")
        self._call(self._env_with_two_queued(), self.NOW - 600)
        tts.synthesize.assert_awaited_once_with("It's 14:34. 3 more songs in the queue.", "en", "ffmpeg")

    def test_broken_timezone_raises_for_the_caller_to_skip(self):
        guild_settings.set_announce("1", True)
        guild_settings.set_timezone("1", "Not/AZone")
        with self.assertRaises(Exception):
            self._call(_Env(), self.NOW - 600)

    def test_attach_speech_wires_the_provider(self):
        env = _Env()
        guild_settings.set_announce("1", True)
        tts_commands.attach_speech(env.bot, 1, env.player)
        with patch.object(tts_commands.time, "time", return_value=self.NOW):
            pcm = asyncio.run(env.speech.between_songs(self.NOW - 600, False))
        self.assertEqual(pcm, CLIP)


class TestOffSwitch(_Base):
    def _help_text(self, enabled):
        sent = []

        async def send(content=None, **kwargs):
            sent.append(content)

        bot = SimpleNamespace(command_prefix="!", tts_enabled=enabled)
        asyncio.run(commands.MusicCog.help_cmd.callback(SimpleNamespace(bot=bot), SimpleNamespace(send=send)))
        return sent[0]

    def test_help_lists_tts_only_when_enabled(self):
        self.assertIn("!tts <text>", self._help_text(True))
        self.assertIn("!ttsallow", self._help_text(True))
        self.assertNotIn("!tts", self._help_text(False))

    def test_help_fits_discords_message_limit(self):
        # Discord refuses a message over 2000 characters, and the five TTS lines take the
        # list past that. !! is the prefix in production.
        for prefix in ("!", "!!"):
            for enabled in (True, False):
                sent = []

                async def send(content=None, **kwargs):
                    sent.append(content)

                bot = SimpleNamespace(command_prefix=prefix, tts_enabled=enabled)
                asyncio.run(commands.MusicCog.help_cmd.callback(SimpleNamespace(bot=bot), SimpleNamespace(send=send)))
                with self.subTest(prefix=prefix, enabled=enabled):
                    self.assertTrue(all(len(m) <= 2000 for m in sent), [len(m) for m in sent])
                    self.assertTrue(sent[0].startswith("**Available commands:**"))
                    self.assertTrue(sent[-1].endswith(f"`{prefix}shutdown` — Shut down the bot *(owner only)*"))
                    if not enabled:
                        self.assertEqual(len(sent), 1)       # unchanged: one message

    def test_setup_loads_the_tts_cog_only_when_enabled(self):
        refreshers = ("_refresh_weather_for", "_refresh_sky_for", "_refresh_f1_if_stale",
                      "_refresh_rocket_if_stale", "_refresh_kp_if_stale")
        for enabled, expected in ((True, 2), (False, 1)):
            bot = SimpleNamespace(tts_enabled=enabled, add_cog=AsyncMock(), config={})
            patches = [patch.object(commands, name, AsyncMock()) for name in refreshers]
            for p in patches:
                p.start()
            try:
                asyncio.run(commands.setup(bot))
            finally:
                for p in patches:
                    p.stop()
            self.assertEqual(bot.add_cog.await_count, expected)
            if enabled:
                self.assertIsInstance(bot.add_cog.await_args_list[1].args[0], tts_commands.TtsCog)

    def test_new_guild_state_gets_the_provider_only_when_enabled(self):
        import main
        for enabled in (True, False):
            bot = main.MusicBot.__new__(main.MusicBot)
            bot.config, bot._guild_states, bot.tts_enabled = {}, {}, enabled
            fake = lambda config: SimpleNamespace(speech=SimpleNamespace(between_songs=None))
            with patch.object(main, "AudioPlayer", fake):
                gs = bot.get_guild_state(5)
            self.assertEqual(gs.player.speech.between_songs is not None, enabled)
