"""A start that a stop or a newer start overtakes gives up with CancelledError.

AudioPlayer.play() and play_radio() raise asyncio.CancelledError right after the gap-speech
wait when the start is no longer current: a stop, or a newer start, overtook it (with or
without text-to-speech). A caller's `except Exception` cleanup never sees it, so a caller
holding something nothing else will clean up does that in an `except asyncio.CancelledError`
and re-raises. It sends no error line: whatever overtook the start owns the card and the chain.

The playlist start with nothing playing stops the background enumeration of the rest of the
list, which would otherwise keep paging YouTube for a list nobody will load.

Network and Discord are faked.
"""
import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import commands
from test_playlist_start import CHANNEL_ID, FIRST, PLAYLIST, RESOLVED, _Bot, _Player, _VC, _view
from track_queue import TrackQueue


class _OvertakenPlayer(_Player):
    """play() gives up as AudioPlayer's start does once a stop or a newer start overtook it."""

    async def play(self, query, resolved_info=None, resolved_at=0.0):
        self.events.append("play")
        raise asyncio.CancelledError


class TestOvertakenPlaylistStart(unittest.TestCase):

    def setUp(self):
        self.clear_np = AsyncMock()
        self.start_auto_next = MagicMock()
        playlist = {"title": "P", "tracks": [{"url": FIRST, "title": "First entry"}]}
        for p in (patch.object(commands, "build_player_view", MagicMock(side_effect=_view)),
                  patch.object(commands, "send_new_np", AsyncMock()),
                  patch.object(commands, "update_np_embed", AsyncMock()),
                  patch.object(commands, "_clear_np", self.clear_np),
                  patch.object(commands, "_start_auto_next", self.start_auto_next),
                  patch.object(commands, "check_channel", AsyncMock(return_value=True)),
                  patch.object(commands, "extract_playlist_info", return_value=playlist)):
            p.start()
            self.addCleanup(p.stop)

    def test_a_cancelled_first_start_stops_the_list(self):
        async def scenario():
            events, cancelled = [], []
            vc = _VC(True)
            gs = SimpleNamespace(queue=TrackQueue(), player=_OvertakenPlayer(events, vc),
                                 auto_next_task=None, auto_next_gen=1, autoplay=False,
                                 current_text_channel_id=None, np_message_id=None)
            bot = _Bot(gs, SimpleNamespace(id=1, voice_client=vc))
            ctx = SimpleNamespace(bot=bot, interaction=None,
                                  guild=SimpleNamespace(id=1, voice_client=vc),
                                  author=SimpleNamespace(id=42, voice=SimpleNamespace(channel=vc.channel)),
                                  channel=SimpleNamespace(id=CHANNEL_ID), send=bot.record)

            async def resolve(*args, **kwargs):
                return dict(RESOLVED)

            async def enumerate_(query, client):
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    cancelled.append(query)
                    raise

            with patch.object(commands, "resolve_audio", resolve), \
                    patch.object(commands, "enumerate_playlist", enumerate_):
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(commands.MusicCog.play.callback(
                        SimpleNamespace(bot=bot), ctx, query=PLAYLIST), timeout=5)
            await asyncio.sleep(0)              # let the cancellation land
            # A copy: closing the loop cancels whatever still runs, and an enumeration left
            # paging would then land in `cancelled` too.
            return bot, events, list(cancelled)

        bot, events, cancelled = asyncio.run(scenario())
        self.assertIn("play", events)
        self.assertEqual(cancelled, [PLAYLIST], "the enumeration kept paging for nobody")
        self.assertFalse(any("error playing first track" in (m or "").lower() for m in bot.sent),
                         bot.sent)
        self.clear_np.assert_not_awaited()      # a stop or the newer start owns the card
        self.start_auto_next.assert_not_called()


class _Message:
    """A message the bot sent: records its edits and whether it was deleted."""

    def __init__(self):
        self.edits = []
        self.deleted = False

    async def edit(self, content=None, **kwargs):
        self.edits.append(content)

    async def delete(self):
        self.deleted = True


class _OvertakenRadioPlayer:
    def __init__(self, vc):
        self._voice_client = vc
        self.is_playing = False
        self.stations = []

    async def play_radio(self, track):
        self.stations.append(track.url)
        raise asyncio.CancelledError


class TestOvertakenStartsClearTheirLoadingMessage(unittest.TestCase):
    """The pickers show "Loading…" once a choice is made, and stop their view, so its timeout
    never clears them. A Spotify start posts "Loading <list>…" first. An overtaken start must
    not leave either message up."""

    def setUp(self):
        self.send_new_np = AsyncMock()
        self.start_auto_next = MagicMock()
        for p in (patch.object(commands, "build_player_view", MagicMock(side_effect=_view)),
                  patch.object(commands, "send_new_np", self.send_new_np),
                  patch.object(commands, "_start_auto_next", self.start_auto_next)):
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _guild(player):
        gs = SimpleNamespace(queue=TrackQueue(), player=player, auto_next_task=None,
                             auto_next_gen=1)
        vc = player._voice_client
        bot = _Bot(gs, SimpleNamespace(id=1, voice_client=vc))
        ctx = SimpleNamespace(bot=bot, guild=SimpleNamespace(id=1, voice_client=vc),
                              author=SimpleNamespace(id=42, voice=SimpleNamespace(channel=vc.channel)),
                              channel=SimpleNamespace(id=CHANNEL_ID))
        return bot, ctx

    def test_an_overtaken_radio_start_clears_its_picker(self):
        station = {"name": "Station", "url_resolved": "https://radio.example/stream"}

        async def scenario():
            player = _OvertakenRadioPlayer(_VC(True))
            bot, ctx = self._guild(player)
            picker = _Message()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(
                    commands._play_radio_selected(bot, ctx, station, picker), timeout=5)
            return player, picker

        player, picker = asyncio.run(scenario())
        self.assertEqual(player.stations, [station["url_resolved"]])
        self.assertTrue(picker.deleted, "the radio picker stayed on 'Loading…'")
        self.assertEqual(picker.edits, [], "an overtaken start is no error")
        self.send_new_np.assert_not_awaited()
        self.start_auto_next.assert_not_called()

    def test_an_overtaken_search_pick_clears_its_picker(self):
        async def scenario():
            events = []
            player = _OvertakenPlayer(events, _VC(True))
            bot, ctx = self._guild(player)
            picker = _Message()

            async def resolve(*args, **kwargs):
                return dict(RESOLVED)

            with patch.object(commands, "resolve_audio", resolve):
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(commands._play_selected(
                        bot, ctx, {"url": FIRST, "title": "First song"}, picker), timeout=5)
            return events, picker

        events, picker = asyncio.run(scenario())
        self.assertEqual(events, ["play"])
        self.assertTrue(picker.deleted, "the search picker stayed on 'Loading…'")
        self.assertEqual(picker.edits, [], "an overtaken start is no error")
        self.send_new_np.assert_not_awaited()
        self.start_auto_next.assert_not_called()

    def test_an_overtaken_spotify_start_removes_its_loading_line(self):
        resolved = {"title": "Mix", "tracks": [{"query": "a - one", "title": "one"},
                                               {"query": "b - two", "title": "two"}]}

        async def scenario():
            events, sent = [], []
            player = _OvertakenPlayer(events, _VC(True))
            bot, ctx = self._guild(player)

            async def send(content=None, **kwargs):
                sent.append((content, _Message()))
                return sent[-1][1]

            async def resolve(*args, **kwargs):
                return dict(RESOLVED)

            ctx.send = send
            with patch.object(commands, "resolve_audio", resolve):
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(commands.MusicCog._queue_spotify(
                        SimpleNamespace(bot=bot), ctx, bot.get_guild_state(1), resolved), timeout=5)
            return events, sent

        events, sent = asyncio.run(scenario())
        self.assertEqual(events, ["play"])
        self.assertEqual(len(sent), 1, sent)
        content, status = sent[0]
        self.assertTrue(content.startswith("Loading"), content)
        self.assertTrue(status.deleted, "the Spotify start left its 'Loading…' line up")
        self.assertEqual(status.edits, [], "an overtaken start is no error")
        self.send_new_np.assert_not_awaited()
        self.start_auto_next.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
