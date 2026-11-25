"""AudioPlayer + Speech: gap speech plays before the song, music is always wrapped."""
import asyncio, os, sys, threading, time, unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audio_player, commands, speech
from track_queue import Track, TrackQueue
from test_play_abort import FRAME, INFO, _FakeVoice, _Harness

CLIP = FRAME * 3


class _OneAtATimeVoice(_FakeVoice):
    """Refuses a second play() like discord.py; finish() is the current source running out."""

    def __init__(self):
        super().__init__()
        self.plays, self.source, self.after, self.stops = [], None, None, 0

    def is_playing(self):
        return self.source is not None

    def play(self, source, after=None, **encoder_settings):
        if self.source is not None:
            raise discord.ClientException("Already playing audio.")
        self.played = self.source = source
        self.after = after
        self.plays.append(source)

    def stop(self):
        self.stops += 1
        self.finish()

    def finish(self):
        source, after = self.source, self.after
        self.source = self.after = None
        if source is not None and after:
            after(None)


class _LeavingVoice(_OneAtATimeVoice):
    """discord.py 2.7.1's disconnect(): the connection reads as closed before its first await,
    and the guild drops the client only once Discord confirms the leave."""

    def __init__(self, guild, channel):
        super().__init__()
        self.guild, self.channel, self.connected = guild, channel, True

    def is_connected(self):
        return self.connected

    def play(self, source, after=None, **encoder_settings):
        if not self.connected:
            raise discord.ClientException("Not connected to voice.")
        super().play(source, after, **encoder_settings)

    async def disconnect(self, *, force=False):
        self.stop()
        self.connected = False
        await asyncio.sleep(0.05)
        self.guild.voice_client = None


def _player(voice):
    with patch.object(audio_player, "_find_ffmpeg", return_value="ffmpeg"):
        player = audio_player.AudioPlayer({"audio": {"sample_rate": 48000, "channels": 2},
                                           "youtube": {"client": "web"}})
    player._voice_client = voice
    return player


class TestPlayerSpeech(unittest.TestCase):
    def test_music_is_wrapped_in_a_mixer(self):
        harness, voice = _Harness(), _OneAtATimeVoice()
        player = _player(voice)
        self.addCleanup(lambda: [b.cleanup() for b in harness.buffers])
        with harness.patched():
            asyncio.run(player.play("q", prefetched_info=dict(INFO)))
        self.assertIsInstance(voice.played, speech.SpeechMixer)
        self.assertIs(voice.played.music, harness.buffers[0])
        self.assertIsNotNone(player.speech.track_started_at)

    def test_after_song_speech_plays_before_the_next_song(self):
        harness, voice = _Harness(), _OneAtATimeVoice()
        player = _player(voice)
        self.addCleanup(lambda: [b.cleanup() for b in harness.buffers])

        async def go():
            player.speech.queue_after(CLIP)
            task = asyncio.ensure_future(player.play("q", prefetched_info=dict(INFO)))
            for _ in range(200):
                await asyncio.sleep(0.01)
                if voice.plays:
                    break
            self.assertIsNone(voice.plays[0].music)
            await asyncio.sleep(0.2)
            self.assertFalse(task.done(), "the song must wait for the gap speech")
            voice.finish()
            await asyncio.wait_for(task, 5)
            self.assertEqual(len(voice.plays), 2)
            self.assertIs(voice.plays[1].music, harness.buffers[0])
            self.assertEqual(voice.stops, 0, "play()'s own stop must not cut the gap speech")

        with harness.patched():
            asyncio.run(go())

    def test_explicit_stop_cuts_speech(self):
        voice = _OneAtATimeVoice()
        player = _player(voice)

        async def go():
            await player.speech.say(CLIP)
            player.stop_playback()
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertEqual(voice.stops, 1)
            self.assertFalse(player.speech.solo_active)

        asyncio.run(go())

    def test_failed_start_plays_held_clips_alone(self):
        voice = _OneAtATimeVoice()
        player = _player(voice)
        gate = threading.Event()

        def failing_open(*args):
            gate.wait(5)
            raise audio_player.StreamFailedError("dead track")

        async def go():
            task = asyncio.ensure_future(player.play("q", prefetched_info=dict(INFO)))
            await asyncio.sleep(0.05)
            self.assertEqual(await player.speech.say(CLIP), "held")
            gate.set()
            with self.assertRaises(audio_player.StreamFailedError):
                await task
            self.assertEqual(len(voice.plays), 1)
            self.assertIsNone(voice.plays[0].music)

        with patch.object(audio_player, "_open_audio_stream", failing_open):
            asyncio.run(go())

    def test_new_voice_client_resets_speech(self):
        player = _player(_OneAtATimeVoice())
        player.speech.queue_after(CLIP)
        player.set_voice_client(_OneAtATimeVoice())
        self.assertEqual(player.speech.waiting_clips(), 0)

    def test_radio_is_wrapped_too(self):
        class _Radio(discord.AudioSource):
            _process = SimpleNamespace(stderr=[])

            def read(self):
                return FRAME

            def is_opus(self):
                return False

        voice = _OneAtATimeVoice()
        player = _player(voice)
        track = Track(query="r", title="R", requested_by="u", url="http://radio", is_radio=True)
        with patch.object(discord, "FFmpegPCMAudio", lambda *a, **k: _Radio()):
            asyncio.run(player.play_radio(track))
        self.assertIsInstance(voice.played, speech.SpeechMixer)


class TestStopDuringGapSpeech(unittest.TestCase):
    """!stop while the next song waits out the gap speech. The stop ends the speech, which
    releases the start onto a connection that is closing. The auto-next chain must not take
    that for a voice drop: put the track back, rejoin the channel and play it."""

    def test_stop_ends_auto_play_for_good(self):
        harness, rejoins, sent, outcome = _Harness(), [], [], {}
        self.addCleanup(lambda: [b.cleanup() for b in harness.buffers])
        guild = SimpleNamespace(id=1, voice_client=None)

        async def connect():
            rejoins.append(_LeavingVoice(guild, channel))
            guild.voice_client = rejoins[-1]
            return rejoins[-1]

        async def send(content=None, **kwargs):
            sent.append(content)

        channel = SimpleNamespace(members=[SimpleNamespace(bot=False)], connect=connect)
        guild.voice_client = voice = _LeavingVoice(guild, channel)
        player = _player(voice)
        gs = SimpleNamespace(queue=TrackQueue(), player=player, auto_next_task=None,
                             auto_next_gen=1, autoplay=False, current_text_channel_id=None)
        gs.queue.add(Track(query="b", title="b", requested_by="u",
                           resolved_info=dict(INFO), resolved_at=time.time()))
        text = SimpleNamespace(id=10, send=send)
        bot = SimpleNamespace(get_guild_state=lambda gid: gs, get_guild=lambda gid: guild,
                              get_channel=lambda cid: text)
        ctx = SimpleNamespace(bot=bot, guild=guild, author=SimpleNamespace(id=42),
                              channel=text, send=send)

        async def scenario():
            parked = asyncio.Event()
            real_wait_for_gap = player.speech.wait_for_gap

            async def wait_for_gap(gen):
                parked.set()
                await real_wait_for_gap(gen)

            player.speech.wait_for_gap = wait_for_gap
            player.speech.queue_after(CLIP)
            commands._start_auto_next(bot, 10, 1)   # pops "b"; its start waits out the clip
            chain, outcome["chain gen"] = gs.auto_next_task, gs.auto_next_gen
            await asyncio.wait_for(parked.wait(), 5)
            await commands.MusicCog.stop.callback(SimpleNamespace(bot=bot), ctx)
            await asyncio.wait({chain}, timeout=1)
            outcome["chain ended"] = chain.done()

        with harness.patched(), \
                patch.object(commands, "_VOICE_RECOVERY_POLL", 0.01), \
                patch.object(commands, "check_channel", AsyncMock(return_value=True)), \
                patch.object(commands, "_check_vote", return_value=(True, "")), \
                patch.object(commands, "update_np_stopped", AsyncMock()), \
                patch.object(commands, "send_new_np", AsyncMock()), \
                patch.object(commands, "build_player_view", lambda *a, **k: None), \
                patch.object(commands, "_get_requester_name", lambda *a, **k: "u"), \
                patch.object(commands, "_schedule_prefetch", lambda *a, **k: None), \
                patch.object(commands, "_schedule_autoplay_topup", lambda *a, **k: None):
            asyncio.run(scenario())

        self.assertEqual(rejoins, [], "the chain rejoined the voice channel after !stop")
        self.assertFalse(any("lost the voice connection" in m.lower() for m in sent), sent)
        self.assertTrue(outcome["chain ended"], "the auto-next chain outlived !stop")
        self.assertGreater(gs.auto_next_gen, outcome["chain gen"], "!stop must retire the chain's generation")
        self.assertEqual(gs.queue.list(), [])
