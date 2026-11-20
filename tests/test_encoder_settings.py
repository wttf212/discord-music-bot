"""Tests that every track's Opus encoder gets the guild's bitrate and the music tuning.

discord.py's VoiceClient.play() builds a brand-new opus.Encoder for each PCM source
from its own keyword arguments, or from their defaults: 128 kbps, FEC on, 15 %
expected packet loss and signal type 'auto'. The player tuned the previous track's
encoder just before calling play(), so that tuning was thrown away. Every track
started at the defaults, and !bitrate changed only the track already playing.

No network: the stream, FFmpeg and the voice client are fakes.
"""
import asyncio
import inspect
import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
import audio_player

GUILD = 42
FRAME = b"\x00" * audio_player._PCM_FRAME_SIZE
WATCH = "https://www.youtube.com/watch?v=x"
# Handed to play() as a fresh prefetch, so nothing is resolved.
INFO = {"title": "T", "url": "https://r1.googlevideo.com/videoplayback?id=x&expire=99999999999",
        "webpage_url": WATCH}
CONFIG = {"audio": {"sample_rate": 48000, "channels": 2, "bitrate": 128}, "debug": False,
          "youtube": {"client": "web_embedded"}}
_PLAY = inspect.signature(discord.VoiceClient.play)


class _Encoder:
    """Records its settings the way opus.Encoder's setters would change them."""

    def __init__(self, **settings):
        self.settings = settings

    def set_bitrate(self, kbps):
        self.settings["bitrate"] = min(512, max(16, int(kbps)))

    def set_fec(self, enabled=True):
        self.settings["fec"] = enabled

    def set_expected_packet_loss_percent(self, percentage):
        self.settings["expected_packet_loss"] = percentage

    def set_bandwidth(self, req):
        self.settings["bandwidth"] = req

    def set_signal_type(self, req):
        self.settings["signal_type"] = req


class _FakeVoice:
    """discord.VoiceClient as 2.7 treats a PCM source: play() binds its arguments to the
    real signature, validates them as opus.Encoder.__init__ does, and replaces
    `encoder` with a new one built from them."""

    def __init__(self):
        self.guild = SimpleNamespace(id=GUILD)
        # The previous track's encoder, with discord.py's defaults.
        self.encoder = _Encoder(bitrate=128, fec=True, expected_packet_loss=0.15,
                                bandwidth="full", signal_type="auto")
        self.built = []      # settings of each encoder play() built, in order
        self.sources = []

    def is_connected(self):
        return True

    def is_playing(self):
        return False

    def is_paused(self):
        return False

    def stop(self):
        pass

    def play(self, source, **kwargs):
        call = _PLAY.bind(self, source, **kwargs)   # an unknown keyword is a TypeError
        call.apply_defaults()
        a = call.arguments
        self.sources.append(source)
        if source.is_opus():
            return
        if not 16 <= a["bitrate"] <= 512:
            raise ValueError(f"bitrate must be between 16 and 512, not {a['bitrate']}")
        if not 0 < a["expected_packet_loss"] <= 1.0:
            raise ValueError(f"bad expected_packet_loss {a['expected_packet_loss']}")
        if a["bandwidth"] not in discord.opus.band_ctl:
            raise KeyError(a["bandwidth"])
        if a["signal_type"] not in discord.opus.signal_ctl:
            raise KeyError(a["signal_type"])
        settings = {k: a[k] for k in ("bitrate", "fec", "bandwidth", "signal_type")}
        if a["fec"]:  # opus.Encoder sets the packet-loss figure only when FEC is on
            settings["expected_packet_loss"] = a["expected_packet_loss"]
        self.encoder = _Encoder(**settings)
        self.built.append(dict(settings))


class _FakeProc:
    stdout = object()
    stderr = None
    pid = -1

    def poll(self):
        return None

    def terminate(self):
        pass

    kill = terminate

    def wait(self, timeout=None):
        return 0


class _FakeFFmpeg(discord.AudioSource):
    def __init__(self, *args, **kwargs):
        self._process = SimpleNamespace(stderr=None)
        self._cleaned = threading.Event()

    def read(self):
        return b"" if self._cleaned.is_set() else FRAME

    def is_opus(self):
        return False

    def cleanup(self):
        self._cleaned.set()


class TestEncoderSettings(unittest.TestCase):

    def setUp(self):
        for p in (patch.object(audio_player, "_find_ffmpeg", return_value="ffmpeg"),
                  patch.object(audio_player, "_open_audio_stream",
                               side_effect=lambda *a, **k: _FakeProc()),
                  patch.object(discord, "FFmpegPCMAudio", _FakeFFmpeg)):
            p.start()
            self.addCleanup(p.stop)
        self.player = audio_player.AudioPlayer(CONFIG)
        self.vc = _FakeVoice()
        self.player.set_voice_client(self.vc)
        self.addCleanup(self._stop_sources)

    def _stop_sources(self):
        for s in self.vc.sources:   # ends the jitter buffers' fill threads
            s.cleanup()

    def _play(self):
        asyncio.run(self.player.play(WATCH, dict(INFO), time.time()))

    def _set_bitrate(self, kbps):
        asyncio.run(self.player.set_bitrate(GUILD, kbps))

    def test_every_track_gets_the_guild_bitrate(self):
        self._set_bitrate(256)
        self._play()
        self._play()
        self.assertEqual([e["bitrate"] for e in self.vc.built], [256, 256])

    def test_every_track_is_tuned_for_music(self):
        self._play()
        self._play()
        for e in self.vc.built:
            self.assertEqual(e["signal_type"], "music")
            self.assertFalse(e["fec"])
            self.assertEqual(e["bandwidth"], "full")
            self.assertNotIn("expected_packet_loss", e, "no FEC, so no packet-loss budget")

    def test_a_bitrate_change_outlives_the_current_track(self):
        self._play()
        self._set_bitrate(320)
        self.assertEqual(self.vc.encoder.settings["bitrate"], 320,
                         "control: !bitrate already retunes the playing track")
        self._play()
        self.assertEqual(self.vc.encoder.settings["bitrate"], 320)

    def test_a_bitrate_below_the_opus_minimum_is_clamped(self):
        """!bitrate accepts 1-15 kbps, which opus.Encoder rejects with a ValueError.
        That must not stop every track in the guild from playing."""
        self._set_bitrate(8)
        self._play()
        self.assertEqual(self.vc.built[-1]["bitrate"], 16)

    def test_radio_gets_the_guild_bitrate_and_music_tuning(self):
        self._set_bitrate(192)
        station = SimpleNamespace(url="http://radio.example/stream", title="R", thumbnail="")
        asyncio.run(self.player.play_radio(station))
        self.assertEqual(self.vc.built[-1]["bitrate"], 192)
        self.assertEqual(self.vc.built[-1]["signal_type"], "music")
        self.assertFalse(self.vc.built[-1]["fec"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
