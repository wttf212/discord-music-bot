"""AudioPlayer + Speech: gap speech plays before the song, music is always wrapped."""
import asyncio, os, sys, threading, unittest
from types import SimpleNamespace
from unittest.mock import patch

import discord

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audio_player, speech
from track_queue import Track
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
