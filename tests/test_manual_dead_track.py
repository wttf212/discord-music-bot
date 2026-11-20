"""Tests that a manual advance onto a dead track keeps auto-play going.

◄/► on the card, !!skip and !!skipto cancel the auto-next chain before they play
the track they picked. When that track turned out to be dead (removed, private,
age-gated, geo-blocked), the handler posted "Skipping track" and stopped there.
Nothing re-armed the chain, so auto-play stayed off, and the rest of the queue
waited until someone skipped again.

The real _auto_next chain runs here against a fake player, so each test sees what
plays after the failed advance.
"""
import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from yt_dlp.utils import DownloadError

import commands
from track_queue import Track

REMOVED = DownloadError("ERROR: [youtube] abc: Video unavailable. "
                        "This video has been removed by the uploader")
FLAKY = DownloadError("ERROR: [youtube] abc: HTTP Error 503: Service Unavailable")
CHANNEL_ID = 999
HANDLERS = ("prev", "next", "skip", "skipto")


class _Spin(BaseException):
    """Escapes _auto_next's `except Exception`, so a spinning chain fails the test."""


class _VC:
    channel = SimpleNamespace(members=[SimpleNamespace(bot=False)])   # nobody leaves

    def is_connected(self):
        return True

    def is_playing(self):
        return False

    def is_paused(self):
        return False


class _Player:
    """Every track plays except the dead ones, which fail every time, the way YouTube
    answers for a removed video. Once `plays` tracks have played, the chain is
    cancelled, the way !!stop cancels it."""

    def __init__(self, dead=(), error=REMOVED, plays=0, during_play=None):
        self._voice_client = _VC()
        self.is_playing = False
        self.dead, self.error, self.plays = set(dead), error, plays
        self.during_play = during_play
        self.calls, self.played = [], []

    async def play(self, query, resolved_info, resolved_at):
        self.calls.append(query)
        if len(self.calls) > 20:
            raise _Spin(f"auto-next is spinning: {self.calls}")
        if self.during_play:
            self.during_play()
        if query in self.dead:
            raise self.error
        self.played.append(query)
        return {"title": query, "thumbnail": "", "webpage_url": ""}

    async def wait_for_playback(self):
        if self.plays and len(self.played) >= self.plays:
            raise asyncio.CancelledError

    def stop_playback(self):
        pass


class _Bot:
    config = {"debug": False}
    command_prefix = "!!"

    def __init__(self, gs):
        self._gs = gs
        self._guild = SimpleNamespace(voice_client=gs.player._voice_client)
        self.sent = []
        self._channel = SimpleNamespace(send=self.record)

    async def record(self, content=None, **kwargs):
        self.sent.append(content)

    def get_guild_state(self, guild_id):
        return self._gs

    def get_guild(self, guild_id):
        return self._guild

    def get_channel(self, channel_id):
        return self._channel


def _track(name):
    return Track(query=name, title=name, requested_by="u")


def _guild_state(player, loop_mode, *, history=(), current=None, queued=()):
    gs = SimpleNamespace(player=player, auto_next_task=None, auto_next_gen=1, autoplay=False,
                         current_text_channel_id=None)
    from track_queue import TrackQueue
    gs.queue = TrackQueue()
    gs.queue.loop_mode = loop_mode
    for name in history:
        gs.queue._history.append(_track(name))
    gs.queue.current = _track(current) if current else None
    for name in queued:
        gs.queue.add(_track(name))
    return gs


async def _advance(handler, bot, gs):
    """Run one manual advance, then the chain it left armed (if any) to its end."""
    if handler in ("prev", "next"):
        interaction = MagicMock()
        interaction.guild_id = 1
        interaction.user.id = 42
        interaction.channel.id = CHANNEL_ID
        interaction.channel.send = bot.record
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock(side_effect=bot.record)
        row = SimpleNamespace(view=SimpleNamespace(
            bot=bot, evaluate_vote=AsyncMock(return_value=True)))
        fn = getattr(commands._ControlsRow, f"{handler}_callback")
        await getattr(fn, "callback", fn)(row, interaction, None)
    else:
        ctx = SimpleNamespace(bot=bot, interaction=None, author=SimpleNamespace(id=42),
                              guild=SimpleNamespace(id=1, voice_client=None),
                              channel=SimpleNamespace(id=CHANNEL_ID),
                              send=AsyncMock(side_effect=bot.record))
        cog = SimpleNamespace(bot=bot)
        if handler == "skip":
            await commands.MusicCog.skip.callback(cog, ctx)
        else:
            await commands.MusicCog.skipto.callback(cog, ctx, 1)
    if gs.auto_next_task is not None:
        await asyncio.wait_for(gs.auto_next_task, 5)


class _Harness(unittest.TestCase):

    def setUp(self):
        for p in (patch.object(commands, "check_channel", AsyncMock(return_value=True)),
                  patch.object(commands, "_check_vote", return_value=(True, "")),
                  patch.object(commands, "_on_cooldown", return_value=False),
                  patch.object(commands, "build_player_view", return_value=None),
                  patch.object(commands, "_get_requester_name", return_value="someone"),
                  patch.object(commands, "send_new_np", AsyncMock()),
                  patch.object(commands, "update_np_stopped", AsyncMock()),
                  patch.object(commands, "_schedule_prefetch", return_value=None),
                  patch.object(commands, "_schedule_autoplay_topup", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _landing_on(dead, handler, loop_mode, player, queued=("g1", "g2")):
        """A guild where `handler` lands on the track `dead`: ◄ goes back to it,
        the others move forward to it. `p` is the track playing beforehand."""
        if handler == "prev":
            return _guild_state(player, loop_mode, history=[dead], current="p", queued=queued)
        return _guild_state(player, loop_mode, current="p", queued=(dead,) + tuple(queued))

    def _run(self, handler, gs):
        bot = _Bot(gs)
        asyncio.run(_advance(handler, bot, gs))
        return bot.sent

    @staticmethod
    def _count(sent, needle):
        return sum(needle in (m or "").lower() for m in sent)


class TestManualAdvanceOntoADeadTrack(_Harness):

    def test_auto_play_carries_on_after_the_dead_track(self):
        for handler in HANDLERS:
            for loop_mode in ("off", "track", "queue"):
                with self.subTest(handler=handler, loop=loop_mode):
                    player = _Player(dead={"dead"}, plays=3)
                    gs = self._landing_on("dead", handler, loop_mode, player)
                    sent = self._run(handler, gs)
                    self.assertTrue(player.played, f"auto-play stopped: {player.calls}")
                    self.assertEqual(player.calls.count("dead"), 1, "tried once, not again")
                    self.assertEqual(self._count(sent, "skipping track"), 1, sent)

    def test_loop_off_moves_on_in_queue_order(self):
        player = _Player(dead={"dead"})
        gs = self._landing_on("dead", "next", "off", player)
        self._run("next", gs)
        self.assertEqual(player.calls, ["dead", "g1", "g2"])
        self.assertIn("dead", [t.query for t in gs.queue._history],
                      "loop off files the dead track into history, as auto-next does")

    def test_two_dead_tracks_in_a_row_under_loop_track_do_not_spin(self):
        """The re-armed chain meets the second one itself. Under loop "track" next()
        used to hand it straight back, forever."""
        player = _Player(dead={"dead1", "dead2"}, plays=2)
        gs = self._landing_on("dead1", "next", "track", player, queued=("dead2", "g1"))
        sent = self._run("next", gs)
        self.assertEqual(player.calls, ["dead1", "dead2", "g1", "g1"])
        self.assertEqual(self._count(sent, "skipping track"), 2, sent)

    def test_a_transient_failure_also_hands_back_to_auto_play(self):
        player = _Player(dead={"flaky"}, error=FLAKY)
        gs = self._landing_on("flaky", "skip", "off", player)
        self._run("skip", gs)
        self.assertEqual(player.played, ["g1", "g2"])

    def test_an_advance_that_was_overtaken_leaves_the_chain_alone(self):
        """A newer advance moved on while the dead track's play() ran. The chain is
        the newer advance's to arm, or two play()s would run at once."""
        gs = None

        def newer_advance():
            if gs.queue.current.query == "dead":
                gs.queue.next(force=True)

        player = _Player(dead={"dead"}, during_play=newer_advance)
        gs = self._landing_on("dead", "next", "off", player)
        self._run("next", gs)
        self.assertIsNone(gs.auto_next_task)
        self.assertEqual(player.calls, ["dead"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
