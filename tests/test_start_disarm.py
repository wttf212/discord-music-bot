"""Tests that the start paths outside the manual advances keep auto-next's disarm rule.

Every path that starts playback by hand must cancel the guild's armed auto-next chain and
bump its generation before stop_playback()/play(), as the ◄/► buttons do. A start stops
the current track first, which sets _playback_done: a chain still armed wakes, sees
nothing playing, pops the next queued track and starts a second play() while the first
start is still under way. The two race for the voice client ("Already playing audio"),
and the popped track is lost once the first start re-arms auto-next.

The real _start_auto_next/_auto_next run here against a fake player with AudioPlayer's
timing: a start stops the current track first and flags the new one as playing only
after an await.
"""
import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import commands
from track_queue import Track, TrackQueue

CHANNEL_ID = 10


class _VC:
    channel = SimpleNamespace(members=[SimpleNamespace(bot=False)])   # nobody leaves

    def is_connected(self):
        return True

    def is_playing(self):
        return False

    def is_paused(self):
        return False


class _RacePlayer:
    """Track "A" is playing. Every start logs itself, stops the current track and flags the
    new one as playing after an await, like the real resolve, stream open and first frame."""

    def __init__(self):
        self._voice_client = _VC()
        self.is_playing = True
        self.is_paused = False
        self._done = asyncio.Event()
        self.log = []

    async def wait_for_playback(self):
        if self.is_playing:
            await self._done.wait()

    def stop_playback(self):
        self.is_playing = False
        self._done.set()

    def set_voice_client(self, vc):
        self._voice_client = vc

    async def _start(self, entry, title):
        self.log.append(entry)
        self.stop_playback()
        await asyncio.sleep(0.05)
        self._done.clear()
        self.is_playing = True
        return {"title": title, "thumbnail": "", "webpage_url": ""}

    async def play(self, query, resolved_info=None, resolved_at=0.0):
        return await self._start(f"play:{query}", query)

    async def play_radio(self, track):
        return await self._start(f"radio:{track.title}", track.title)


class _Bot:
    config = {"debug": False, "youtube": {"client": "web"}}

    def __init__(self, gs):
        self._gs = gs
        self._guild = SimpleNamespace(id=1, voice_client=gs.player._voice_client)
        self.sent = []

    def get_guild_state(self, guild_id):
        return self._gs

    def get_guild(self, guild_id):
        return self._guild

    def get_channel(self, channel_id):
        return SimpleNamespace(send=self.record)

    async def record(self, content=None, **kwargs):
        self.sent.append(content)
        return _Msg()


class _Msg:
    async def edit(self, **kwargs):
        pass

    async def delete(self):
        pass


def _ctx(bot):
    return SimpleNamespace(bot=bot, interaction=None,
                           guild=SimpleNamespace(id=1, voice_client=bot._guild.voice_client),
                           author=SimpleNamespace(id=42, voice=SimpleNamespace(channel=None)),
                           channel=SimpleNamespace(id=CHANNEL_ID), send=bot.record)


def _armed_guild():
    """"A" is playing with auto-next armed on it, and "B" is queued. Needs a running loop."""
    player = _RacePlayer()
    gs = SimpleNamespace(queue=TrackQueue(), player=player, auto_next_task=None,
                         auto_next_gen=1, autoplay=False, current_text_channel_id=None)
    gs.queue.add(Track(query="A", title="A", requested_by="u"))
    gs.queue.next()
    gs.queue.add(Track(query="B", title="B", requested_by="u"))
    bot = _Bot(gs)
    commands._start_auto_next(bot, CHANNEL_ID, 1)
    return bot, gs, player


async def _settle(gs):
    """Give a stale chain time to start its play(), then stop the chain that is left."""
    await asyncio.sleep(0.2)
    if gs.auto_next_task is not None:
        gs.auto_next_task.cancel()
        await asyncio.gather(gs.auto_next_task, return_exceptions=True)


class _Patched(unittest.TestCase):

    def setUp(self):
        for p in (patch.object(commands, "build_player_view", return_value=None),
                  patch.object(commands, "_get_requester_name", return_value="someone"),
                  patch.object(commands, "send_new_np", AsyncMock()),
                  patch.object(commands, "update_np_stopped", AsyncMock()),
                  patch.object(commands, "_schedule_prefetch", return_value=None),
                  patch.object(commands, "_schedule_autoplay_topup", return_value=None)):
            p.start()
            self.addCleanup(p.stop)


class TestRadioStart(_Patched):

    def test_starting_a_station_disarms_the_chain(self):
        station = {"url_resolved": "http://radio.example/stream", "name": "Radio X", "favicon": ""}

        async def scenario():
            bot, gs, player = _armed_guild()
            await asyncio.sleep(0)                      # the chain is waiting on "A"
            await commands._play_radio_selected(bot, _ctx(bot), station, _Msg())
            await _settle(gs)
            return gs, player

        gs, player = asyncio.run(scenario())
        self.assertEqual(player.log, ["radio:Radio X"],
                         "the armed chain started a second play() during the station's start")
        self.assertEqual([t.query for t in gs.queue.list()], ["B"])
        self.assertTrue(gs.queue.current.is_radio)


if __name__ == "__main__":
    unittest.main(verbosity=2)
