"""Tests that slash commands answer their interaction in time.

Discord refuses an interaction's first response (a reply or a defer) once 3 s have
passed since the command was used. The reply raises NotFound 10062 ("Unknown
interaction"), and the user sees "The application did not respond".

- /skipto awaited the track's resolve, often more than 3 s when cold, before its
  first reply. That reply raised, the error path's reply raised again, and
  auto-next was never re-armed, so auto-play stopped after the track.
- /setlocation geocoded the city and refreshed the weather before replying.
- A slash command used outside the channel set by !settc got no answer at all.
  check_channel tried to delete a message that doesn't exist and posted its notice
  in the allowed channel.
"""
import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
import audio_player
import commands
from track_queue import Track, TrackQueue

CHANNEL_ID = 10
ALLOWED_ID = 20


def _dead_token():
    resp = MagicMock()
    resp.status = 404
    resp.reason = "Not Found"
    return discord.NotFound(resp, {"code": 10062, "message": "Unknown interaction"})


class _Interaction:
    """A slash-command interaction. Once `expired` is set, Discord refuses the first
    response (a reply or a defer). Followups to an acknowledged one still work."""

    def __init__(self, expired=False):
        self.expired = expired
        self.acked = False
        self.replies = []    # (content, ephemeral)
        self.response = SimpleNamespace(is_done=lambda: self.acked, defer=self._defer,
                                        send_message=self._first_response)
        self.followup = SimpleNamespace(send=self._followup)

    async def _defer(self, **kwargs):
        if self.expired:
            raise _dead_token()
        self.acked = True

    async def _first_response(self, content=None, **kwargs):
        if self.expired:
            raise _dead_token()
        self.acked = True
        self.replies.append((content, kwargs.get("ephemeral", False)))

    async def _followup(self, content=None, **kwargs):
        self.replies.append((content, kwargs.get("ephemeral", False)))


class _Ctx:
    """A hybrid command's context. send() routes like commands.Context.send: through
    the interaction when there is one, else to the channel."""

    def __init__(self, bot, interaction=None):
        self.bot = bot
        self.interaction = interaction
        self.guild = SimpleNamespace(id=1, voice_client=None)
        self.channel = SimpleNamespace(id=CHANNEL_ID)
        self.author = SimpleNamespace(id=42, mention="<@42>")
        self.message = SimpleNamespace(delete=AsyncMock())
        self.channel_sent = []

    async def send(self, content=None, **kwargs):
        it = self.interaction
        if it is None:
            self.channel_sent.append(content)
        elif it.response.is_done():
            await it.followup.send(content, **kwargs)
        else:
            await it.response.send_message(content, **kwargs)


class _Player:
    def __init__(self, during_play=None, error=None):
        self.is_playing = False
        self._during_play = during_play
        self._error = error

    async def play(self, query, resolved_info, resolved_at):
        if self._during_play:
            self._during_play()
        if self._error is not None:
            raise self._error
        return {"title": query, "thumbnail": "", "webpage_url": ""}

    def stop_playback(self):
        pass


class _Bot:
    command_prefix = "!!"

    def __init__(self, gs=None):
        self.config = {"owner_id": "42", "debug": False}
        self._gs = gs
        self.allowed_channel = SimpleNamespace(send=AsyncMock())

    def get_guild_state(self, guild_id):
        return self._gs

    def get_guild(self, guild_id):
        return None

    def get_channel(self, channel_id):
        return self.allowed_channel if channel_id == ALLOWED_ID else None


def _guild_state(player):
    gs = SimpleNamespace(queue=TrackQueue(), player=player, auto_next_task=None,
                         auto_next_gen=0)
    for name in ("t1", "t2", "t3"):
        gs.queue.add(Track(query=name, title=name, requested_by="42"))
    return gs


class _Patched(unittest.TestCase):

    allowed_channel = None

    def setUp(self):
        self.start_auto_next = MagicMock()
        for p in (patch.object(commands, "get_allowed_channel",
                               side_effect=lambda gid: self.allowed_channel),
                  patch.object(commands, "_live_voice_client", return_value=object()),
                  patch.object(commands, "build_player_view", return_value=None),
                  patch.object(commands, "_get_requester_name", return_value="someone"),
                  patch.object(commands, "send_new_np", AsyncMock()),
                  patch.object(commands, "_start_auto_next", self.start_auto_next)):
            p.start()
            self.addCleanup(p.stop)


class TestSkiptoSlash(_Patched):

    def _skipto(self, it):
        """/skipto 2, with a resolve that outlasts the interaction's 3 s window."""
        gs = _guild_state(_Player(during_play=lambda: setattr(it, "expired", True)))
        ctx = _Ctx(_Bot(gs), it)
        try:
            asyncio.run(commands.MusicCog.skipto.callback(SimpleNamespace(bot=ctx.bot), ctx, 2))
        except discord.HTTPException as e:
            return e
        return None

    def test_a_slow_resolve_still_gets_its_reply(self):
        it = _Interaction()
        self.assertIsNone(self._skipto(it))
        self.assertIn(("Jumped to **t2**.", False), it.replies)
        self.start_auto_next.assert_called_once()

    def test_auto_play_is_rearmed_even_if_every_reply_fails(self):
        it = _Interaction(expired=True)   # already dead when the command started
        self._skipto(it)
        self.start_auto_next.assert_called_once()

    def test_a_start_handed_back_to_auto_play_still_answers(self):
        """A YouTube block, or voice dropping during the play(), keeps the track and hands it
        to auto-play, which tells the channel why it waits. The handler stays quiet for a
        prefix command, but a deferred slash command shows "is thinking…" until answered."""
        from yt_dlp.utils import DownloadError
        refusals = {
            "shared cooldown": (audio_player.YouTubeBlockedError(60.0), object()),
            "bot check": (DownloadError("ERROR: [youtube] abc123: Sign in to confirm you’re "
                                        "not a bot."), object()),
            "voice drop": (discord.ClientException("Not connected to voice."), None),
        }
        for name, (error, voice_after) in refusals.items():
            for slash in (True, False):
                with self.subTest(name, slash=slash):
                    self.start_auto_next.reset_mock()
                    gs = _guild_state(_Player(error=error))
                    it = _Interaction() if slash else None
                    ctx = _Ctx(_Bot(gs), it)
                    # healthy for the check before the advance, then as play() left it
                    with patch.object(commands, "_live_voice_client",
                                      side_effect=[object(), voice_after]):
                        asyncio.run(commands.MusicCog.skipto.callback(
                            SimpleNamespace(bot=ctx.bot), ctx, 2))
                    self.start_auto_next.assert_called_once()
                    self.assertEqual([t.query for t in gs.queue.list()], ["t2", "t3"])
                    if slash:
                        self.assertEqual(len(it.replies), 1, it.replies)
                        self.assertNotIn("skipping", it.replies[0][0].lower())
                    else:
                        self.assertEqual(ctx.channel_sent, [], "the chain tells the channel")


class TestSetlocationSlash(_Patched):

    def test_slow_geocoding_still_gets_its_reply(self):
        it = _Interaction()

        def geocode(city):
            it.expired = True     # the geocoding round trip outlasts the 3 s window
            return ("Riga", 56.95, 24.11)

        ctx = _Ctx(_Bot(SimpleNamespace(queue=TrackQueue())), it)
        with patch.object(commands.weather, "geocode", geocode), \
                patch.object(commands, "set_weather_location"), \
                patch.object(commands, "_refresh_weather_for", AsyncMock()), \
                patch.object(commands, "_refresh_sky_for", AsyncMock()):
            asyncio.run(commands.MusicCog.setlocation.callback(
                commands.MusicCog(ctx.bot), ctx, city="Riga"))
        self.assertIn(("Weather location set to **Riga**.", False), it.replies)


class TestRestrictedChannel(_Patched):

    allowed_channel = str(ALLOWED_ID)

    def test_a_slash_command_gets_a_private_answer(self):
        it = _Interaction()
        bot = _Bot()
        self.assertFalse(asyncio.run(commands.check_channel(_Ctx(bot, it))))
        self.assertEqual(it.replies, [(f"Please use commands in <#{ALLOWED_ID}>.", True)])
        bot.allowed_channel.send.assert_not_awaited()

    def test_a_prefix_command_is_still_deleted_and_redirected(self):
        bot = _Bot()
        ctx = _Ctx(bot)
        self.assertFalse(asyncio.run(commands.check_channel(ctx)))
        ctx.message.delete.assert_awaited_once()
        bot.allowed_channel.send.assert_awaited_once_with(
            f"<@42>, please use commands in <#{ALLOWED_ID}>.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
