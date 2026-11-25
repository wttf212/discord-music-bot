"""!!play <playlist> with nothing audible starts track 1 the way a single track starts.

Only track 1 has to wait for anything; the rest of the list loads in the background. The
start used to look as if it waited for the whole list: "Loading playlist…" stayed up through
the voice join, track 1's resolve and its CDN settle, so by the time a card appeared a
normal-sized list (122 tracks in the reported case) had already finished and the card showed
its full count. Track 1's card now shows as soon as its entry is known, and its resolve runs
while voice connects. Network and Discord are faked.
"""
import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_player
import commands
from track_queue import Track, TrackQueue

CHANNEL_ID = 10
PLAYLIST = "https://www.youtube.com/playlist?list=PLxxxxxxxxxxxxxxxx"
FIRST = "https://www.youtube.com/watch?v=fffffffffff"
SECOND = "https://www.youtube.com/watch?v=sssssssssss"
RESOLVED = {"title": "First song", "webpage_url": FIRST, "thumbnail": "",
            "url": "https://r1.googlevideo.com/videoplayback?id=x&expire=99999999999"}
TWO_TRACKS = {"title": "P", "tracks": [{"url": FIRST, "title": "First entry"},
                                       {"url": SECOND, "title": "Second entry"}]}


class _Msg:
    def __init__(self, sent):
        self._sent = sent

    async def edit(self, content=None, **kwargs):
        if content is not None:
            self._sent.append(content)

    async def delete(self):
        pass


class _VC:
    def __init__(self, connected, channel=None):
        self.connected = connected
        self.channel = channel or SimpleNamespace(members=[SimpleNamespace(bot=False)])

    def is_connected(self):
        return self.connected

    def is_playing(self):
        return False

    def is_paused(self):
        return False

    async def disconnect(self, force=False):
        self.connected = False


class _Player:
    def __init__(self, events, vc):
        self.events = events
        self._voice_client = vc
        self.is_playing = False
        self.is_paused = False
        self.play_args = None

    def set_voice_client(self, vc):
        self._voice_client = vc

    def stop_playback(self):
        pass

    async def play(self, query, resolved_info=None, resolved_at=0.0):
        self.events.append("play")
        self.play_args = (query, resolved_info, resolved_at)
        self.is_playing = True
        return {"title": RESOLVED["title"], "thumbnail": "", "webpage_url": FIRST}


class _Bot:
    config = {"debug": False, "youtube": {"client": "web"}}
    command_prefix = "!!"

    def __init__(self, gs, guild):
        self._gs = gs
        self._guild = guild
        self.pending_playlists = {}
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


def _view(*args, **kwargs):
    """build_player_view stand-in: keeps what the card was built with, and whether the
    playlist offer was armed at that moment (that is what renders the Load button)."""
    return SimpleNamespace(args=args, kwargs=kwargs, offer_armed=bool(args[0].pending_playlists))


class TestPlaylistStartsLikeASingleTrack(unittest.TestCase):

    def setUp(self):
        self.events = []
        self.edits = []
        self.views = MagicMock(side_effect=_view)
        self.send_new_np = AsyncMock(side_effect=lambda *a, **k: self.events.append("card"))
        self.update_np_embed = AsyncMock(side_effect=self._edit)
        self.clear_np = AsyncMock(side_effect=lambda *a, **k: self.events.append("clear"))
        self.start_auto_next = MagicMock()
        self.playlist = {"title": "P", "tracks": [{"url": FIRST, "title": "First entry"}]}
        for p in (patch.object(commands, "build_player_view", self.views),
                  patch.object(commands, "send_new_np", self.send_new_np),
                  patch.object(commands, "update_np_embed", self.update_np_embed),
                  patch.object(commands, "_clear_np", self.clear_np),
                  patch.object(commands, "_start_auto_next", self.start_auto_next),
                  patch.object(commands, "check_channel", AsyncMock(return_value=True)),
                  patch.object(commands, "_schedule_prefetch", return_value=None),
                  patch.object(commands, "get_bitrate", return_value=None),
                  patch.object(commands, "get_eq_bass", return_value=0),
                  patch.object(commands, "get_eq_treble", return_value=0),
                  patch.object(commands, "extract_playlist_info",
                               side_effect=lambda *a, **k: self.playlist)):
            p.start()
            self.addCleanup(p.stop)

    def _edit(self, bot, channel_id, view, **kwargs):
        self.events.append("edit")
        self.edits.append((view, kwargs))

    def _guild(self, connected=True):
        vc = _VC(connected)
        player = _Player(self.events, vc)
        gs = SimpleNamespace(queue=TrackQueue(), player=player, auto_next_task=None,
                             auto_next_gen=1, autoplay=False, current_text_channel_id=None,
                             np_message_id=None)
        bot = _Bot(gs, SimpleNamespace(id=1, voice_client=vc))
        voice_channel = vc.channel if connected else SimpleNamespace(connect=None)
        ctx = SimpleNamespace(bot=bot, interaction=None,
                              guild=SimpleNamespace(id=1, voice_client=vc),
                              author=SimpleNamespace(id=42, voice=SimpleNamespace(channel=voice_channel)),
                              channel=SimpleNamespace(id=CHANNEL_ID), send=bot.record)
        return bot, gs, player, ctx

    async def _play(self, bot, ctx, resolve, enumerate_):
        # The command must run INSIDE the patches; returning the coroutine from the block
        # would run it after they are undone, against the real network.
        with patch.object(commands, "resolve_audio", resolve), \
                patch.object(commands, "enumerate_playlist", enumerate_):
            await commands.MusicCog.play.callback(SimpleNamespace(bot=bot), ctx, query=PLAYLIST)

    @staticmethod
    async def _until(cond, steps=200):
        for _ in range(steps):
            if cond():
                return True
            await asyncio.sleep(0.005)
        return cond()

    def test_track_one_card_shows_before_its_resolve_finishes(self):
        async def scenario():
            bot, gs, player, ctx = self._guild()
            gate = asyncio.Event()

            async def resolve(*args, **kwargs):
                self.events.append("resolve")
                await gate.wait()
                return dict(RESOLVED)

            async def enumerate_(query, client):
                return TWO_TRACKS

            task = asyncio.create_task(self._play(bot, ctx, resolve, enumerate_))
            shown = await self._until(lambda: "card" in self.events)
            before_release = list(self.events)
            gate.set()
            await asyncio.wait_for(task, timeout=5)
            return shown, before_release, player

        shown, before_release, player = asyncio.run(scenario())
        self.assertTrue(shown, "no card while track 1 was still resolving")
        self.assertNotIn("play", before_release)
        loading = self.views.call_args_list[0]
        self.assertTrue(loading.kwargs.get("loading"), "the first card is not the loading card")
        self.assertEqual(loading.args[1], "First entry", "the card should name track 1 at once")
        self.assertEqual(loading.args[2], commands._loading_card_text("P"),
                         "the loading card has no Load button, so no 'click' line either")
        self.assertEqual(loading.kwargs.get("thumbnail"), commands._youtube_thumbnail(FIRST))
        self.send_new_np.assert_awaited_once()          # one card, edited in place afterwards
        query, info, resolved_at = player.play_args
        self.assertEqual((query, info["url"]), (FIRST, RESOLVED["url"]),
                         "play() must reuse the resolve that overlapped the voice join")
        self.assertGreater(resolved_at, 0)
        # The list landed before track 1 played, so exactly one edit follows play(): the
        # live card with the real count, built after the offer was armed (Load button).
        self.assertEqual(self.events[self.events.index("play"):].count("edit"), 1, self.events)
        live, _ = self.edits[-1]
        self.assertFalse(live.kwargs.get("loading"))
        self.assertEqual(live.args[1], RESOLVED["title"])
        self.assertTrue(live.offer_armed, "the live card was built before the offer was armed")

    def test_track_one_resolve_runs_while_voice_connects(self):
        async def scenario():
            bot, gs, player, ctx = self._guild(connected=False)

            async def connect():
                self.events.append("connect")
                await asyncio.sleep(0.05)
                self.events.append("connected")
                return _VC(True)

            ctx.author.voice.channel.connect = connect

            async def resolve(*args, **kwargs):
                self.events.append("resolve")
                await asyncio.sleep(0.05)
                return dict(RESOLVED)

            async def enumerate_(query, client):
                return TWO_TRACKS

            await asyncio.wait_for(self._play(bot, ctx, resolve, enumerate_), timeout=5)

        asyncio.run(scenario())
        self.assertLess(self.events.index("resolve"), self.events.index("connected"),
                        f"track 1's resolve waited for the voice join: {self.events}")
        self.assertIn("play", self.events)

    def test_track_one_plays_while_the_list_is_still_loading(self):
        async def scenario():
            bot, gs, player, ctx = self._guild()
            released = asyncio.Event()

            async def resolve(*args, **kwargs):
                return dict(RESOLVED)

            async def enumerate_(query, client):
                await released.wait()           # a long list, still paging
                return TWO_TRACKS

            task = asyncio.create_task(self._play(bot, ctx, resolve, enumerate_))
            played = await self._until(lambda: "play" in self.events and "edit" in self.events)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return played

        self.assertTrue(asyncio.run(scenario()),
                        "track 1 did not start while the list was still loading")
        live, kwargs = self.edits[0]
        self.assertFalse(live.kwargs.get("loading"))
        self.assertTrue(live.offer_armed, "the live card must carry the Load button")
        self.assertTrue(kwargs.get("wait"),
                        "the live edit must land before _finalize_offer's count edit")
        self.start_auto_next.assert_called_once()

    def test_a_failed_resolve_clears_the_card_and_stops_the_list(self):
        async def scenario():
            bot, gs, player, ctx = self._guild()
            cancelled = []

            async def resolve(*args, **kwargs):
                raise audio_player.YouTubeBlockedError(60.0)

            async def enumerate_(query, client):
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    cancelled.append(query)
                    raise

            # A start that played anyway would await the never-ending list: fail, don't hang.
            await asyncio.wait_for(self._play(bot, ctx, resolve, enumerate_), timeout=5)
            await asyncio.sleep(0)              # let the cancellation land
            return bot, cancelled

        bot, cancelled = asyncio.run(scenario())
        self.assertNotIn("play", self.events)
        self.clear_np.assert_awaited_once()     # no "Loading…" card left behind
        self.assertEqual(cancelled, [PLAYLIST], "the enumeration kept paging for nobody")
        self.start_auto_next.assert_called_once()   # a block is no fault of the playlist
        self.assertTrue(any("error playing first track" in (m or "").lower() for m in bot.sent),
                        bot.sent)

    def test_a_failure_leaves_another_starts_card_alone(self):
        """Another start put its own card up while track 1 was resolving; this start's
        failure must not delete that card."""
        async def scenario():
            bot, gs, player, ctx = self._guild()

            async def send_card(*args, **kwargs):
                gs.np_message_id = 100              # this start's loading card
                self.events.append("card")

            self.send_new_np.side_effect = send_card

            async def resolve(*args, **kwargs):
                gs.np_message_id = 101              # someone else's card replaced it
                raise audio_player.YouTubeBlockedError(60.0)

            async def enumerate_(query, client):
                return TWO_TRACKS

            await asyncio.wait_for(self._play(bot, ctx, resolve, enumerate_), timeout=5)
            return bot

        bot = asyncio.run(scenario())
        self.clear_np.assert_not_awaited()
        self.assertTrue(any("error playing first track" in (m or "").lower() for m in bot.sent))

    def test_an_untitled_first_entry_shows_loading(self):
        """SoundCloud set entries come back from flat extraction titled 'Unknown'."""
        self.playlist = {"title": "Set", "tracks": [{"url": "https://soundcloud.com/a/one",
                                                     "title": "Unknown"}]}

        async def scenario():
            bot, gs, player, ctx = self._guild()

            async def resolve(*args, **kwargs):
                return dict(RESOLVED)

            async def enumerate_(query, client):
                return {"title": "Set", "tracks": []}

            await asyncio.wait_for(self._play(bot, ctx, resolve, enumerate_), timeout=5)

        asyncio.run(scenario())
        loading = self.views.call_args_list[0]
        self.assertEqual(loading.args[1], "Loading…")
        self.assertEqual(loading.kwargs.get("thumbnail"), "")


class _ChainPlayer:
    """AudioPlayer's timing, for the real auto-next chain: play() fails while voice is down,
    otherwise stops the current track, awaits (stream open), then flags the new one playing."""

    def __init__(self, vc, log):
        self._voice_client = vc
        self.is_playing = False
        self.is_paused = False
        self._done = asyncio.Event()
        self._done.set()
        self.log = log

    def set_voice_client(self, vc):
        self._voice_client = vc

    async def wait_for_playback(self):
        if self.is_playing:
            await self._done.wait()

    def stop_playback(self):
        self.is_playing = False
        self._done.set()

    async def play(self, query, resolved_info=None, resolved_at=0.0):
        if not self._voice_client or not self._voice_client.is_connected():
            self.log.append(f"play:{query}:voice-down")
            raise RuntimeError("Not connected to a voice channel")
        self.log.append(f"play:{query}")
        self.stop_playback()
        await asyncio.sleep(0.05)
        self._done.clear()
        self.is_playing = True
        return {"title": query, "thumbnail": "", "webpage_url": query}


class TestTheStartAndTheAutoPlayChain(unittest.TestCase):
    """The playlist start disarms auto-next twice: when the voice join returns, and again
    right before play(). The real _start_auto_next/_auto_next run here.

    The first disarm: auto-play is waiting out a voice drop with the track it kept at the
    queue front. The start's own voice join brings voice back; if the chain were still armed
    while track 1 resolves, it would resume, start the kept track, and then be cut off by the
    start — losing the kept track (it ends up in neither queue, history nor current).

    The second: a manual advance that finishes while track 1 is resolving (a !!skip) plays its
    track and arms a chain on it. play() stops that track, which wakes the chain; without the
    second disarm it starts a second play() alongside the playlist's."""

    def setUp(self):
        for p in (patch.object(commands, "build_player_view", MagicMock(return_value=None)),
                  patch.object(commands, "send_new_np", AsyncMock()),
                  patch.object(commands, "update_np_embed", AsyncMock()),
                  patch.object(commands, "update_np_stopped", AsyncMock()),
                  patch.object(commands, "_clear_np", AsyncMock()),
                  patch.object(commands, "_get_requester_name", return_value="someone"),
                  patch.object(commands, "check_channel", AsyncMock(return_value=True)),
                  patch.object(commands, "_schedule_prefetch", return_value=None),
                  patch.object(commands, "_schedule_autoplay_topup", return_value=None),
                  patch.object(commands, "get_bitrate", return_value=None),
                  patch.object(commands, "get_eq_bass", return_value=0),
                  patch.object(commands, "get_eq_treble", return_value=0),
                  patch.object(commands, "extract_playlist_info",
                               return_value={"title": "P",
                                             "tracks": [{"url": FIRST, "title": "First entry"}]}),
                  patch.object(commands, "_VOICE_RECOVERY_POLL", 0.02)):
            p.start()
            self.addCleanup(p.stop)

    def test_the_kept_track_survives_the_playlist_start(self):
        async def scenario():
            log = []
            vchan = SimpleNamespace(members=[SimpleNamespace(bot=False)], connect=None)
            stale = _VC(False, vchan)
            guild = SimpleNamespace(id=1, voice_client=stale)
            player = _ChainPlayer(stale, log)
            gs = SimpleNamespace(queue=TrackQueue(), player=player, auto_next_task=None,
                                 auto_next_gen=1, autoplay=False, current_text_channel_id=None,
                                 np_message_id=None)
            bot = _Bot(gs, guild)

            async def disconnect(force=False):
                guild.voice_client = None          # discord.py's cleanup drops the client

            stale.disconnect = disconnect

            async def connect():
                new = _VC(False, vchan)
                guild.voice_client = new           # registered before the handshake...
                await asyncio.sleep(0.05)
                new.connected = True               # ...connected only at its end
                return new

            vchan.connect = connect

            # Auto-play's play() met a dead socket: the chain put "kept" back and is polling.
            gs.queue.add(Track(query="kept", title="kept", requested_by="u"))
            commands._start_auto_next(bot, CHANNEL_ID, 1)
            for _ in range(50):
                await asyncio.sleep(0.01)
                if any("Lost the voice connection" in (m or "") for m in bot.sent):
                    break
            self.assertEqual([t.query for t in gs.queue.list()], ["kept"])
            log.append("start")

            async def resolve(*args, **kwargs):
                await asyncio.sleep(0.3)           # track 1's resolve outlasts the join
                return dict(RESOLVED)

            async def enumerate_(query, client):
                return TWO_TRACKS

            ctx = SimpleNamespace(bot=bot, interaction=None, guild=guild,
                                  author=SimpleNamespace(id=42, voice=SimpleNamespace(channel=vchan)),
                                  channel=SimpleNamespace(id=CHANNEL_ID), send=bot.record)
            with patch.object(commands, "resolve_audio", resolve), \
                    patch.object(commands, "enumerate_playlist", enumerate_):
                await asyncio.wait_for(commands.MusicCog.play.callback(
                    SimpleNamespace(bot=bot), ctx, query=PLAYLIST), timeout=5)
            await asyncio.sleep(0.2)
            result = (log[log.index("start"):], [t.query for t in gs.queue.list()],
                      gs.queue.current.query if gs.queue.current else None)
            if gs.auto_next_task:
                gs.auto_next_task.cancel()
                await asyncio.gather(gs.auto_next_task, return_exceptions=True)
            return result

        after_start, queue, current = asyncio.run(scenario())
        self.assertEqual(after_start, ["start", f"play:{FIRST}"],
                         "the waiting chain resumed during the playlist start")
        self.assertEqual(queue, ["kept"], "the track auto-play kept was lost")
        self.assertEqual(current, FIRST)

    def test_a_skip_during_the_resolve_does_not_race_track_one(self):
        async def scenario():
            log = []
            vchan = SimpleNamespace(members=[SimpleNamespace(bot=False)])
            vc = _VC(True, vchan)
            guild = SimpleNamespace(id=1, voice_client=vc)
            player = _ChainPlayer(vc, log)
            gs = SimpleNamespace(queue=TrackQueue(), player=player, auto_next_task=None,
                                 auto_next_gen=1, autoplay=False, current_text_channel_id=None,
                                 np_message_id=None)
            gs.queue.add(Track(query="q2", title="q2", requested_by="u"))
            bot = _Bot(gs, guild)
            gate = asyncio.Event()

            async def resolve(*args, **kwargs):
                log.append("resolving")
                await gate.wait()
                return dict(RESOLVED)

            async def enumerate_(query, client):
                return TWO_TRACKS

            ctx = SimpleNamespace(bot=bot, interaction=None, guild=guild,
                                  author=SimpleNamespace(id=42, voice=SimpleNamespace(channel=vchan)),
                                  channel=SimpleNamespace(id=CHANNEL_ID), send=bot.record)

            async def start():
                with patch.object(commands, "resolve_audio", resolve), \
                        patch.object(commands, "enumerate_playlist", enumerate_):
                    await commands.MusicCog.play.callback(SimpleNamespace(bot=bot), ctx,
                                                          query=PLAYLIST)

            task = asyncio.create_task(start())
            for _ in range(100):
                await asyncio.sleep(0.005)
                if "resolving" in log:
                    break
            # A !!skip lands meanwhile: it plays q1 and arms a chain waiting on q1.
            await player.play("q1")
            gs.queue.current = Track(query="q1", title="q1", requested_by="u")
            commands._start_auto_next(bot, CHANNEL_ID, 1)
            await asyncio.sleep(0.02)
            log.append("release")
            gate.set()
            await asyncio.wait_for(task, timeout=5)
            await asyncio.sleep(0.2)
            result = (log[log.index("release"):], [t.query for t in gs.queue.list()],
                      gs.queue.current.query if gs.queue.current else None)
            if gs.auto_next_task:
                gs.auto_next_task.cancel()
                await asyncio.gather(gs.auto_next_task, return_exceptions=True)
            return result

        after_release, queue, current = asyncio.run(scenario())
        self.assertEqual(after_release, ["release", f"play:{FIRST}"],
                         "a chain armed during the resolve started a second play()")
        self.assertEqual(queue, ["q2"])
        self.assertEqual(current, FIRST)


class _BlockingChannel:
    """A channel whose NP-card edit waits on `gate`, to see whether update_np_embed waits."""

    def __init__(self, gate, log):
        self.guild = SimpleNamespace(id=1)
        self._gate = gate
        self._log = log

    def get_partial_message(self, msg_id):
        channel = self

        class _Message:
            async def edit(self, view=None):
                channel._log.append("edit-start")
                await channel._gate.wait()
                channel._log.append("edit-done")

        return _Message()


class TestUpdateNpEmbedWait(unittest.TestCase):
    """update_np_embed(wait=True) is what keeps the playlist start's live-card edit ahead of
    _finalize_offer's count edit; without it the two can land in either order."""

    def _run(self, wait):
        async def scenario():
            gate, log = asyncio.Event(), []
            channel = _BlockingChannel(gate, log)
            gs = SimpleNamespace(np_message_id=5, np_views=[])
            bot = SimpleNamespace(get_channel=lambda cid: channel, get_guild_state=lambda gid: gs)
            view = object()
            call = asyncio.create_task(commands.update_np_embed(bot, CHANNEL_ID, view, wait=wait))
            await asyncio.sleep(0.02)
            returned_before_the_edit_landed = call.done()
            gate.set()
            await asyncio.wait_for(call, timeout=1)
            await asyncio.sleep(0.01)           # let a background edit finish
            return returned_before_the_edit_landed, log, gs.np_views == [view]

        return asyncio.run(scenario())

    def test_wait_returns_only_after_the_edit_landed(self):
        early, log, stored = self._run(wait=True)
        self.assertFalse(early, "update_np_embed(wait=True) returned before the edit landed")
        self.assertEqual(log, ["edit-start", "edit-done"])
        self.assertTrue(stored, "the edited view must stay registered for its buttons")

    def test_default_does_not_wait(self):
        early, log, stored = self._run(wait=False)
        self.assertTrue(early)
        self.assertEqual(log, ["edit-start", "edit-done"])
        self.assertTrue(stored)


if __name__ == "__main__":
    unittest.main(verbosity=2)
