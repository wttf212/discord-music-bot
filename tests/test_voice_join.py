"""Joining voice restores the server's saved bitrate and EQ.

Every flow that joins the author's voice channel goes through commands._join_voice:
!play and its playlist start, the search and radio pickers, Spotify lists, !tts and
!ttsafter.
After a restart the player knows only the config's default bitrate and a flat EQ, so the
join is where a server gets its saved settings back. The settings file and the player
are real; only the voice channel is faked.

A join that discord.py interrupts mid-handshake fails like any other join, so every one of
those flows reports it.
"""
import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_player
import commands
import guild_settings

GUILD = 42
CONFIG = {"audio": {"sample_rate": 48000, "channels": 2, "bitrate": 128}, "debug": False}


class _Channel:
    """A voice channel whose connect() hands back a new client, as discord.py's does."""

    def __init__(self):
        # Right after connect() a real VoiceClient has its guild and no encoder yet:
        # play() builds one for each track.
        self.client = SimpleNamespace(guild=SimpleNamespace(id=GUILD), encoder=None)

    async def connect(self):
        return self.client


class TestJoinVoiceRestoresSettings(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = patch.object(guild_settings, "SETTINGS_FILE",
                         os.path.join(tmp.name, "guild_settings.json"))
        p.start()
        self.addCleanup(p.stop)
        self.player = audio_player.AudioPlayer(CONFIG)
        self.gs = SimpleNamespace(player=self.player)
        self.ctx = SimpleNamespace(guild=SimpleNamespace(id=GUILD))
        self.channel = _Channel()

    def _join(self):
        return asyncio.run(commands._join_voice(self.ctx, self.gs, self.channel))

    def test_restores_the_saved_bitrate_and_eq(self):
        guild_settings.set_bitrate(str(GUILD), 96)
        guild_settings.set_eq_bass(str(GUILD), 3)
        guild_settings.set_eq_treble(str(GUILD), -2)

        joined = self._join()

        self.assertIs(joined, self.channel.client)
        self.assertIs(self.player._voice_client, self.channel.client)
        self.assertEqual(self.player.get_bitrate_for_guild(GUILD), 96_000)
        self.assertEqual(self.player.get_eq_for_guild(GUILD), (3, -2))

    def test_restores_the_eq_when_only_treble_is_saved(self):
        guild_settings.set_eq_treble(str(GUILD), 4)

        self._join()

        self.assertEqual(self.player.get_eq_for_guild(GUILD), (0, 4))

    def test_keeps_the_defaults_when_nothing_is_saved(self):
        self._join()

        self.assertEqual(self.player.get_bitrate_for_guild(GUILD), 128_000)
        self.assertEqual(self.player.get_eq_for_guild(GUILD), (0, 0))


class _InterruptedChannel:
    """A voice channel whose handshake discord.py interrupts. When the bot is moved, or the
    voice server changes, while it connects, discord.py 2.7.1 restarts the handshake and
    cancels the connector task that connect() awaits. The task awaiting connect() was not
    cancelled itself, yet it gets a CancelledError."""

    async def connect(self):
        connector = asyncio.ensure_future(asyncio.sleep(3600))
        asyncio.get_running_loop().call_soon(connector.cancel)
        return await connector


class _SlowChannel:
    """A voice channel whose handshake is still running."""

    async def connect(self):
        await asyncio.sleep(3600)


class TestInterruptedJoin(unittest.TestCase):
    """A join that discord.py interrupts fails like any other join, so every caller's
    `except Exception` reports it: a slash command answers instead of staying on "is
    thinking…". Cancelling the caller itself (a stop, or shutdown) still cancels."""

    def setUp(self):
        self.player = audio_player.AudioPlayer(CONFIG)
        self.gs = SimpleNamespace(player=self.player)
        self.ctx = SimpleNamespace(guild=SimpleNamespace(id=GUILD))

    def test_an_interrupted_handshake_is_a_failed_join(self):
        with self.assertRaises(ConnectionError):
            asyncio.run(commands._join_voice(self.ctx, self.gs, _InterruptedChannel()))
        self.assertIsNone(self.player._voice_client)    # no half-made client is adopted

    def test_cancelling_the_caller_still_cancels(self):
        async def scenario():
            join = asyncio.ensure_future(commands._join_voice(self.ctx, self.gs, _SlowChannel()))
            await asyncio.sleep(0)                      # into the handshake
            join.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await join

        asyncio.run(scenario())
        self.assertIsNone(self.player._voice_client)


if __name__ == "__main__":
    unittest.main()
