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
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
from yt_dlp.utils import DownloadError

import audio_player
import commands
from track_queue import Track, TrackQueue

CHANNEL_ID = 10
PLAYLIST = "https://www.youtube.com/playlist?list=PLxxxxxxxxxxxxxxxx"
FIRST = "https://www.youtube.com/watch?v=fffffffffff"

# What a start can meet while auto-play waits a YouTube block or a voice drop out, and the
# voice state the handler finds afterwards (None: the socket is down).
REFUSALS = {
    "shared cooldown": (audio_player.YouTubeBlockedError(60.0), object()),
    "bot check": (DownloadError("ERROR: [youtube] abc123: Sign in to confirm you’re not a bot."),
                  object()),
    "voice drop": (discord.ClientException("Not connected to voice."), None),
}
REMOVED = DownloadError("ERROR: [youtube] abc123: Video unavailable. "
                        "This video has been removed by the uploader")


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
        return _Msg(self.sent)


class _Msg:
    """A sent message; edits to its text land in the same list as the sends."""

    def __init__(self, sent=None):
        self._sent = sent if sent is not None else []

    async def edit(self, content=None, **kwargs):
        if content is not None:
            self._sent.append(content)

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


class _FailingPlayer:
    """Nothing audible; play() fails with `error`, if there is one."""

    def __init__(self, error):
        self._voice_client = _VC()
        self.is_playing = False
        self.is_paused = False
        self.error = error

    async def play(self, query, resolved_info=None, resolved_at=0.0):
        if self.error is not None:
            raise self.error
        return {"title": query, "thumbnail": "", "webpage_url": ""}

    def stop_playback(self):
        pass


def _waiting_guild(error):
    """Auto-play is waiting a block or a voice drop out: its chain is armed and the track it
    put back is at the head of the queue."""
    gs = SimpleNamespace(queue=TrackQueue(), player=_FailingPlayer(error), auto_next_gen=5,
                         auto_next_task=MagicMock(), autoplay=False, current_text_channel_id=None,
                         np_message_id=None)
    gs.auto_next_task.done.return_value = False
    gs.queue.requeue_front(Track(query="kept", title="kept", requested_by="u"))
    return gs


class _StartHarness(_Patched):
    """Runs a start path against _waiting_guild with _start_auto_next recorded."""

    def setUp(self):
        super().setUp()
        self.start_auto_next = MagicMock()
        for p in (patch.object(commands, "check_channel", AsyncMock(return_value=True)),
                  patch.object(commands, "_start_auto_next", self.start_auto_next)):
            p.start()
            self.addCleanup(p.stop)

    def _run(self, start, error, voice_after):
        """`start(bot, ctx, gs)` is awaited; returns (gs, the chain it found, what it sent)."""
        self.start_auto_next.reset_mock()
        gs = _waiting_guild(error)
        chain = gs.auto_next_task
        bot = _Bot(gs)
        bot.pending_playlists = {}
        bot.command_prefix = "!!"
        ctx = _ctx(bot)
        ctx.author.voice.channel = ctx.guild.voice_client.channel
        with patch.object(commands, "_live_voice_client", return_value=voice_after):
            asyncio.run(start(bot, ctx, gs))
        return gs, chain, bot.sent


def _playlist_start(resolve_error=None):
    """!!play <playlist> whose track 1 resolve raises `resolve_error`; the full enumeration
    never finishes, so nothing here can depend on it."""
    async def start(bot, ctx, gs):
        async def never_finishes(query, client):
            await asyncio.sleep(3600)

        resolve = AsyncMock(side_effect=resolve_error, return_value=dict(RESOLVED))
        with patch.object(commands, "extract_playlist_info",
                          return_value={"title": "P", "tracks": [{"url": FIRST, "title": "f"}]}), \
                patch.object(commands, "enumerate_playlist", never_finishes), \
                patch.object(commands, "resolve_audio", resolve):
            # Every case here ends the start early; a start that went on to await the
            # never-ending enumeration fails the test instead of hanging it.
            await asyncio.wait_for(commands.MusicCog.play.callback(
                SimpleNamespace(bot=bot), ctx, query=PLAYLIST), timeout=5)
    return start


class TestPlaylistStart(_StartHarness):
    """!!play <playlist> with nothing audible disarms the chain as soon as the voice join
    returns, while track 1's resolve (started before the join) may still be running. A
    YouTube block or a voice drop refusing track 1 — at the resolve or during play() — used
    to leave the chain that was waiting one out cancelled for good, so the track that chain
    kept never played: it is re-armed instead."""

    def test_a_refused_first_track_rearms_auto_play(self):
        for name, (error, voice_after) in REFUSALS.items():
            with self.subTest(name):
                gs, chain, sent = self._run(_playlist_start(), error, voice_after)
                chain.cancel.assert_called_once()          # disarmed before play(), as before
                self.start_auto_next.assert_called_once()
                self.assertEqual(self.start_auto_next.call_args.args[1:], (CHANNEL_ID, 1))
                self.assertEqual([t.query for t in gs.queue.list()], ["kept"])

    def test_a_refused_resolve_rearms_auto_play(self):
        gs, chain, sent = self._run(_playlist_start(audio_player.YouTubeBlockedError(60.0)),
                                    None, object())
        chain.cancel.assert_called_once()
        self.start_auto_next.assert_called_once()
        self.assertEqual([t.query for t in gs.queue.list()], ["kept"])

    def test_a_dead_first_track_is_reported_as_before(self):
        for name, (resolve_error, play_error) in {"at the resolve": (REMOVED, None),
                                                  "during play()": (None, REMOVED)}.items():
            with self.subTest(name):
                gs, chain, sent = self._run(_playlist_start(resolve_error), play_error, object())
                self.start_auto_next.assert_not_called()
                self.assertEqual([t.query for t in gs.queue.list()], ["kept"])
                self.assertTrue(any("error playing first track" in (m or "").lower()
                                    for m in sent), sent)


SPOTIFY = {"title": "SP", "tracks": [{"query": "S1", "title": "S1"}, {"query": "S2", "title": "S2"}]}
RESOLVED = {"title": "S1", "webpage_url": "",
            "url": "https://r1.googlevideo.com/videoplayback?id=x&expire=99999999999"}


def _spotify_start(resolve_error=None):
    """A start that runs _queue_spotify for SPOTIFY; track 1's resolve raises `resolve_error`."""
    async def start(bot, ctx, gs):
        resolve = AsyncMock(side_effect=resolve_error, return_value=dict(RESOLVED))
        with patch.object(commands, "resolve_audio", resolve):
            await commands.MusicCog._queue_spotify(SimpleNamespace(bot=bot), ctx, gs, SPOTIFY)
    return start


class TestSpotifyStart(_Patched):
    """A multi-track Spotify link with nothing audible plays its first track by hand."""

    def test_starting_the_first_track_disarms_the_chain(self):
        async def scenario():
            bot, gs, player = _armed_guild()
            await asyncio.sleep(0)                      # the chain is waiting on "A"
            await _spotify_start()(bot, _ctx(bot), gs)
            await _settle(gs)
            return gs, player

        gs, player = asyncio.run(scenario())
        self.assertEqual(player.log, ["play:S1"],
                         "the armed chain started a second play() during the Spotify start")
        self.assertEqual([t.query for t in gs.queue.list()], ["B", "S2"])


class TestSpotifyStartRefused(_StartHarness):
    """Track 1 is resolved before the disarm, as the single-track path does, so a track that
    can't be found or a YouTube block leaves a waiting chain alone. A refusal during play(),
    after the disarm, re-arms it as a refused playlist start does."""

    def _assert_chain_kept(self, chain):
        self.assertFalse(chain.cancel.called and not self.start_auto_next.called,
                         "the chain waiting the refusal out was cancelled and never re-armed")

    def test_a_refused_first_track_keeps_auto_play(self):
        """Refused at the resolve, before anything is touched, or during play()."""
        cases = {f"{name} during play()": (None, error, voice)
                 for name, (error, voice) in REFUSALS.items()}
        cases["block at the resolve"] = (audio_player.YouTubeBlockedError(60.0), None, object())
        for name, (resolve_error, play_error, voice_after) in cases.items():
            with self.subTest(name):
                gs, chain, sent = self._run(_spotify_start(resolve_error), play_error, voice_after)
                self._assert_chain_kept(chain)
                self.assertEqual([t.query for t in gs.queue.list()], ["kept"])

    def test_a_first_track_that_cannot_be_found_touches_nothing(self):
        gs, chain, sent = self._run(_spotify_start(REMOVED), None, object())
        chain.cancel.assert_not_called()
        self.start_auto_next.assert_not_called()
        self.assertEqual([t.query for t in gs.queue.list()], ["kept"])
        self.assertTrue(any("error playing first track" in (m or "").lower() for m in sent), sent)


if __name__ == "__main__":
    unittest.main(verbosity=2)
