"""Joining voice restores the server's saved bitrate and EQ.

Every flow that joins the author's voice channel goes through commands._join_voice:
!play and its playlist start, the search and radio pickers, Spotify lists, !tts and
!ttsafter.
After a restart the player knows only the config's default bitrate and a flat EQ, so the
join is where a server gets its saved settings back. The settings file and the player
are real; only the voice channel is faked.
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


if __name__ == "__main__":
    unittest.main()
