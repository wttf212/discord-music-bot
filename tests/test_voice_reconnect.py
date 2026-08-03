"""Tests for surviving a voice-socket drop (WebSocket 1006) during auto-next.

A dead voice socket used to be misclassified as a retryable *track* error: play()
raised RuntimeError("Not connected to a voice channel"), which the auto-next loop
read as "bad track", burning three queued songs and tripping the circuit breaker
while discord.py was still reconnecting in the background. These tests cover the
requeue-and-wait fix plus the AudioPlayer.disconnect() leak it depends on.
"""
import asyncio
import logging
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_player
import commands
from track_queue import TrackQueue, Track


class TestRequeueFront(unittest.TestCase):

    def test_requeue_front_loop_off(self):
        q = TrackQueue()
        a = Track(query="a", title="a", requested_by="u")
        q.add(a)
        self.assertIs(q.next(), a)
        self.assertIs(q.current, a)

        q.requeue_front(a)
        self.assertIsNone(q.current)
        self.assertEqual(len(q), 1)
        self.assertIs(q.next(), a)
        self.assertEqual(len(q), 0)
        self.assertEqual(list(q._history), [], "the track never played; it must not land in history")

    def test_requeue_front_loop_track(self):
        q = TrackQueue()
        q.loop_mode = "track"
        a = Track(query="a", title="a", requested_by="u")
        q.add(a)
        self.assertIs(q.next(), a)

        q.requeue_front(a)
        self.assertIsNone(q.current)
        self.assertEqual(len(q), 1)
        # loop "track" returns self.current without popping *unless* current is None,
        # in which case it falls through to the normal pop path.
        self.assertIs(q.next(), a)
        self.assertEqual(len(q), 0, "must not leave a duplicate copy sitting in the queue")

    def test_requeue_front_loop_queue(self):
        q = TrackQueue()
        q.loop_mode = "queue"
        a = Track(query="a", title="a", requested_by="u")
        q.add(a)
        self.assertIs(q.next(), a)

        q.requeue_front(a)
        self.assertIsNone(q.current)
        self.assertEqual(len(q), 1)
        self.assertIs(q.next(), a)
        self.assertEqual(len(q), 0, "must not append a second copy to the back")


class _FakeVC:
    def __init__(self, connected: bool):
        self._connected = connected
        self.disconnect_called = False

    def is_connected(self):
        return self._connected

    def is_playing(self):
        return False

    def is_paused(self):
        return False

    async def disconnect(self):
        self.disconnect_called = True


class TestDisconnectClearsVoiceClient(unittest.TestCase):

    def setUp(self):
        self._ffmpeg_patch = patch.object(audio_player, "_find_ffmpeg", return_value="ffmpeg")
        self._ffmpeg_patch.start()
        self.addCleanup(self._ffmpeg_patch.stop)
        self._config = {"audio": {"sample_rate": 48000, "channels": 2}, "debug": False}

    def test_disconnect_clears_already_dead_client(self):
        async def run():
            p = audio_player.AudioPlayer(self._config)
            vc = _FakeVC(connected=False)
            p._voice_client = vc
            await p.disconnect()
            return p, vc

        p, vc = asyncio.run(run())
        self.assertFalse(vc.disconnect_called)
        self.assertIsNone(p._voice_client, "the leak: a dead client must still be cleared")

    def test_disconnect_awaits_live_client(self):
        async def run():
            p = audio_player.AudioPlayer(self._config)
            vc = _FakeVC(connected=True)
            p._voice_client = vc
            await p.disconnect()
            return p, vc

        p, vc = asyncio.run(run())
        self.assertTrue(vc.disconnect_called)
        self.assertIsNone(p._voice_client)


class _Member:
    def __init__(self, bot=False):
        self.bot = bot


class _VoiceChannel:
    """Non-empty channel so the queue-drained auto-leave branch stays a no-op here."""

    def __init__(self, members=None, connect_result=None, connect_error=None, on_connect=None):
        self.members = members if members is not None else [_Member(bot=False)]
        self.connect_result = connect_result
        self.connect_error = connect_error
        self.on_connect = on_connect
        self.connect_calls = 0

    async def connect(self):
        self.connect_calls += 1
        if self.on_connect:
            self.on_connect()
        if self.connect_error is not None:
            raise self.connect_error
        if self.connect_result is not None:
            return self.connect_result
        return _VC(connected=True, channel=self)


class _VC:
    """Fake discord.py VoiceClient tracking connection state across polls."""

    def __init__(self, connected: bool, flip_after: int | None = None, on_check=None, channel=None):
        self._connected = connected
        self.flip_after = flip_after
        self.on_check = on_check
        self.check_count = 0
        self.channel = channel if channel is not None else _VoiceChannel()

    def is_connected(self):
        self.check_count += 1
        if self.on_check:
            self.on_check(self.check_count)
        if self.flip_after is not None and self.check_count >= self.flip_after:
            self._connected = True
        return self._connected

    def is_playing(self):
        return False

    def is_paused(self):
        return False


class _Player:
    """Fake AudioPlayer: play() raises exactly like the real one when voice is down."""

    def __init__(self, vc, error=None):
        self.is_playing = False
        self._voice_client = vc
        self.play_calls = []
        self._error = error
        self.stop_calls = 0

    async def play(self, query, resolved_info, resolved_at):
        if self._error is not None:
            raise self._error
        if not (self._voice_client and self._voice_client.is_connected()):
            raise RuntimeError("Not connected to a voice channel")
        self.play_calls.append(query)
        return {"title": query, "thumbnail": "", "webpage_url": ""}

    def set_voice_client(self, vc):
        self._voice_client = vc

    async def wait_for_playback(self):
        return

    def stop_playback(self):
        self.stop_calls += 1


class _Guild:
    def __init__(self, voice_client):
        self.voice_client = voice_client


class _Channel:
    def __init__(self):
        self.sent = []

    async def send(self, content):
        self.sent.append(content)


class _GS:
    def __init__(self, player):
        self.queue = TrackQueue()
        self.player = player
        self.auto_next_task = None
        self.auto_next_gen = 1
        self.autoplay = False
        self.prefetch_task = None
        self.autoplay_task = None
        self.autoplay_pool = []
        self.autoplay_history = set()
        self.current_text_channel_id = 10
        self.fairness_pct = 50


class _Bot:
    config = {"debug": False}

    def __init__(self, gs, guild):
        self._gs = gs
        self._guild = guild
        self._channel = _Channel()

    def get_guild_state(self, guild_id):
        return self._gs

    def get_guild(self, guild_id):
        return self._guild

    def get_channel(self, channel_id):
        return self._channel


# --- Fakes for the four manual advance handlers (◄/► buttons, !!skip, !!skipto) ---
# Fixed, distinct channel ids so a test can assert _start_auto_next re-armed on the
# handler's own channel, not a stray default.
_BUTTON_CHANNEL_ID = 555
_CTX_CHANNEL_ID = 999


class _Ctx:
    """Fake prefix-command context for !!skip / !!skipto."""

    def __init__(self, bot):
        self.bot = bot
        self.guild = SimpleNamespace(id=1, voice_client=None)
        self.channel = SimpleNamespace(id=_CTX_CHANNEL_ID)
        self.author = SimpleNamespace(id=42)
        self.sent = []

    async def send(self, content, **kwargs):
        self.sent.append(content)


class _View:
    """Fake PlayerView passed as `self.view` to the ◄/► button callbacks."""

    def __init__(self, bot):
        self.bot = bot
        self._kwargs = {}

    async def evaluate_vote(self, interaction, action):
        return True


def _interaction():
    """A MagicMock interaction wired like tests/test_interaction_ack.py's, with a
    channel.send that records into the same list as followup/response replies, so
    every user-visible string the handler produced ends up in one place."""
    it = MagicMock()
    it.guild_id = 1
    it.user.id = 42
    sent = []

    async def _record(content, **kwargs):
        sent.append(content)

    it.channel = MagicMock()
    it.channel.id = _BUTTON_CHANNEL_ID
    it.channel.send = _record
    it.response.is_done.return_value = False
    it.response.defer = AsyncMock()
    it.response.send_message = AsyncMock(side_effect=_record)
    it.followup.send = AsyncMock(side_effect=_record)
    return it, sent


async def _invoke(handler, bot, gs, *, position=3):
    """Dispatch on the four manual-advance handler names, invoking the real handler
    body without constructing discord.py objects (the pattern already proven in
    tests/test_interaction_ack.py: `getattr(fn, "callback", fn)`). Returns every
    user-visible string the handler produced.
    """
    if handler in ("prev", "next"):
        interaction, sent = _interaction()
        row = SimpleNamespace(view=_View(bot))
        fn = commands._ControlsRow.prev_callback if handler == "prev" else commands._ControlsRow.next_callback
        body = getattr(fn, "callback", fn)
        await body(row, interaction, None)
        return sent

    ctx = _Ctx(bot)
    cog = SimpleNamespace(bot=bot)
    if handler == "skip":
        await commands.MusicCog.skip.callback(cog, ctx)
    else:
        await commands.MusicCog.skipto.callback(cog, ctx, position)
    return ctx.sent


class TestAutoNextVoiceRecovery(unittest.TestCase):

    def setUp(self):
        self._patches = [
            patch.object(commands, "build_player_view", return_value=None),
            patch.object(commands, "_get_requester_name", return_value="someone"),
            patch.object(commands, "send_new_np", side_effect=self._noop),
            patch.object(commands, "update_np_stopped", side_effect=self._noop),
            patch.object(commands, "_schedule_prefetch", return_value=None),
            patch.object(commands, "_schedule_autoplay_topup", return_value=None),
            patch.object(commands, "_VOICE_RECOVERY_TIMEOUT", 0.05),
            patch.object(commands, "_VOICE_RECOVERY_POLL", 0.01),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    async def _noop(*a, **k):
        return None

    def test_voice_down_requeues_track_and_spares_the_breaker(self):
        vc = _VC(connected=False)
        player = _Player(vc)
        gs = _GS(player)
        track = Track(query="a", title="a", requested_by="u")
        gs.queue.add(track)
        bot = _Bot(gs, _Guild(vc))

        asyncio.run(commands._auto_next(bot, 10, 1, 1))

        self.assertEqual(player.play_calls, [], "play() must never succeed while voice is down")
        self.assertEqual(gs.queue.list(), [track], "the track must be back in the queue")
        self.assertIsNone(gs.queue.current)
        sent = bot._channel.sent
        self.assertEqual(sum("lost the voice connection" in m.lower() for m in sent), 1)
        self.assertEqual(sum("didn't come back" in m.lower() or "did not come back" in m.lower()
                              for m in sent), 1)
        self.assertFalse(any("skipping track" in m.lower() for m in sent))
        self.assertFalse(any("consecutive errors" in m.lower() for m in sent))
        self.assertEqual(vc.channel.connect_calls, 0,
                          "discord.py is still retrying — we must not race it with our own connect()")

    def test_playback_resumes_when_voice_comes_back(self):
        with patch.object(commands, "_VOICE_RECOVERY_TIMEOUT", 5.0):
            vc = _VC(connected=False, flip_after=3)
            player = _Player(vc)
            gs = _GS(player)
            track = Track(query="a", title="a", requested_by="u")
            gs.queue.add(track)
            bot = _Bot(gs, _Guild(vc))

            asyncio.run(commands._auto_next(bot, 10, 1, 1))

        self.assertEqual(player.play_calls, ["a"], "the same track must resume once voice is back")
        self.assertIsNone(gs.queue.current, "queue drained after the resumed track finished")
        self.assertEqual(len(gs.queue), 0)
        sent = bot._channel.sent
        self.assertEqual(sum("lost the voice connection" in m.lower() for m in sent), 1)
        self.assertFalse(any("come back" in m.lower() and "didn't" in m.lower() for m in sent),
                          "no give-up message on a successful recovery")

    def test_generation_change_aborts_the_wait_cleanly(self):
        vc = _VC(connected=False)
        player = _Player(vc)
        gs = _GS(player)
        track = Track(query="a", title="a", requested_by="u")
        gs.queue.add(track)
        bot = _Bot(gs, _Guild(vc))

        def bump_generation(check_count):
            if check_count == 1:
                gs.auto_next_gen = 2

        vc.on_check = bump_generation

        asyncio.run(commands._auto_next(bot, 10, 1, 1))

        self.assertEqual(player.play_calls, [])
        self.assertEqual(gs.queue.list(), [track], "the track must stay in the queue")
        sent = bot._channel.sent
        self.assertFalse(any("come back" in m.lower() and ("didn't" in m.lower() or "did not" in m.lower())
                              for m in sent), "aborted-by-generation must not give up out loud")

    def test_healthy_voice_still_skips_and_trips_the_breaker(self):
        from yt_dlp.utils import DownloadError
        error = DownloadError("ERROR: [youtube] HTTP Error 429: Too Many Requests")
        vc = _VC(connected=True)
        player = _Player(vc, error=error)
        gs = _GS(player)
        tracks = [Track(query=f"t{i}", title=f"t{i}", requested_by="u") for i in range(4)]
        for t in tracks:
            gs.queue.add(t)
        bot = _Bot(gs, _Guild(vc))

        asyncio.run(commands._auto_next(bot, 10, 1, 1))

        sent = bot._channel.sent
        self.assertEqual(sum("skipping track" in m.lower() for m in sent), 3)
        self.assertEqual(sum("too many consecutive errors" in m.lower() for m in sent), 1)
        # 3 of the 4 queued tracks were attempted (and lost) before the breaker tripped;
        # none of them come back to the front of the queue the way a voice-down requeue would.
        self.assertEqual(gs.queue.list(), [tracks[3]])


class TestSelfReconnect(unittest.TestCase):
    """discord.py has run cleanup() and dropped the voice client entirely — polling
    can never succeed, so the auto-next chain must reconnect itself, bounded and
    generation-safe. In every test the guild is `_Guild(None)` (discord.py gave up)
    and the stale channel hint comes from `gs.player._voice_client.channel`.
    """

    def setUp(self):
        self._patches = [
            patch.object(commands, "build_player_view", return_value=None),
            patch.object(commands, "_get_requester_name", return_value="someone"),
            patch.object(commands, "send_new_np", side_effect=self._noop),
            patch.object(commands, "update_np_stopped", side_effect=self._noop),
            patch.object(commands, "_schedule_prefetch", return_value=None),
            patch.object(commands, "_schedule_autoplay_topup", return_value=None),
            patch.object(commands, "_VOICE_RECOVERY_TIMEOUT", 5.0),
            patch.object(commands, "_VOICE_RECOVERY_POLL", 0.01),
            patch.object(commands, "_VOICE_RECONNECT_BACKOFF", 0.01),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    async def _noop(*a, **k):
        return None

    def test_client_gone_self_connects_once_and_resumes(self):
        channel = _VoiceChannel()
        vc = _VC(connected=False, channel=channel)
        player = _Player(vc)
        gs = _GS(player)
        track = Track(query="a", title="a", requested_by="u")
        gs.queue.add(track)
        bot = _Bot(gs, _Guild(None))

        asyncio.run(commands._auto_next(bot, 10, 1, 1))

        self.assertEqual(channel.connect_calls, 1)
        self.assertEqual(player.play_calls, ["a"])
        self.assertIsNone(gs.queue.current)
        self.assertEqual(len(gs.queue), 0)
        sent = bot._channel.sent
        self.assertFalse(any("didn't come back" in m.lower() or "did not come back" in m.lower()
                              for m in sent))

    def test_empty_channel_is_not_rejoined(self):
        channel = _VoiceChannel(members=[_Member(bot=True)])
        vc = _VC(connected=False, channel=channel)
        player = _Player(vc)
        gs = _GS(player)
        track = Track(query="a", title="a", requested_by="u")
        gs.queue.add(track)
        bot = _Bot(gs, _Guild(None))

        asyncio.run(commands._auto_next(bot, 10, 1, 1))

        self.assertEqual(channel.connect_calls, 0)
        self.assertEqual(player.play_calls, [])
        self.assertEqual(gs.queue.list(), [track], "the track must stay in the queue")

    def test_generation_change_during_connect_aborts(self):
        channel = _VoiceChannel()
        vc = _VC(connected=False, channel=channel)
        player = _Player(vc)
        gs = _GS(player)
        track = Track(query="a", title="a", requested_by="u")
        gs.queue.add(track)
        bot = _Bot(gs, _Guild(None))

        def bump_generation():
            gs.auto_next_gen = 2

        channel.on_connect = bump_generation

        asyncio.run(commands._auto_next(bot, 10, 1, 1))

        self.assertEqual(player.play_calls, [])
        self.assertEqual(gs.queue.list(), [track], "the track must stay in the queue")
        self.assertIsNone(gs.player._voice_client,
                           "the superseded generation must not adopt the reconnected client")
        sent = bot._channel.sent
        self.assertFalse(any("didn't come back" in m.lower() or "did not come back" in m.lower()
                              for m in sent), "an aborted-by-generation connect must not give up out loud")

    def test_connect_failure_does_not_trip_the_breaker(self):
        channel = _VoiceChannel(connect_error=RuntimeError("boom"))
        vc = _VC(connected=False, channel=channel)
        player = _Player(vc)
        gs = _GS(player)
        track = Track(query="a", title="a", requested_by="u")
        gs.queue.add(track)
        bot = _Bot(gs, _Guild(None))

        asyncio.run(commands._auto_next(bot, 10, 1, 1))

        self.assertGreaterEqual(channel.connect_calls, 1)
        self.assertLessEqual(channel.connect_calls, commands._VOICE_RECONNECT_ATTEMPTS)
        self.assertEqual(player.play_calls, [])
        self.assertEqual(gs.queue.list(), [track], "the track must stay in the queue")
        sent = bot._channel.sent
        self.assertEqual(sum("didn't come back" in m.lower() or "did not come back" in m.lower()
                              for m in sent), 1)
        self.assertFalse(any("skipping track" in m.lower() for m in sent))
        self.assertFalse(any("consecutive errors" in m.lower() for m in sent))


class TestManualAdvanceVoiceGuard(unittest.TestCase):
    """Fix 1: prev/next/skip/skipto must refuse the advance outright when voice is
    down, before touching the auto-next chain, the player or the queue — the exact
    state a frustrated user's repeated skip clicks used to destroy (three queued
    tracks lost to a manual handler that disarmed auto-next and then failed).
    """

    HANDLERS = ("prev", "next", "skip", "skipto")

    def setUp(self):
        self._patches = [
            patch.object(commands, "check_channel", side_effect=self._true),
            patch.object(commands, "_check_vote", return_value=(True, "")),
            patch.object(commands, "_on_cooldown", return_value=False),
            patch.object(commands, "build_player_view", return_value=None),
            patch.object(commands, "_get_requester_name", return_value="someone"),
            patch.object(commands, "send_new_np", side_effect=self._noop),
            patch.object(commands, "update_np_stopped", side_effect=self._noop),
            patch.object(commands, "_schedule_prefetch", return_value=None),
            patch.object(commands, "_schedule_autoplay_topup", return_value=None),
            patch.object(commands, "_start_auto_next", MagicMock()),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    async def _true(ctx):
        return True

    @staticmethod
    async def _noop(*a, **k):
        return None

    @staticmethod
    def _seed_refused(handler, gs):
        """Seed enough queue state that a missing guard would be detectable."""
        if handler == "prev":
            for i in range(3):
                gs.queue._history.append(Track(query=f"h{i}", title=f"h{i}", requested_by="u"))
            gs.queue.current = Track(query="cur", title="cur", requested_by="u")
        elif handler == "skipto":
            for i in range(3):
                gs.queue.add(Track(query=f"q{i}", title=f"q{i}", requested_by="u"))
        else:
            for i in range(2):
                gs.queue.add(Track(query=f"q{i}", title=f"q{i}", requested_by="u"))

    def test_voice_down_refuses_and_touches_nothing(self):
        for handler in self.HANDLERS:
            with self.subTest(handler=handler):
                commands._start_auto_next.reset_mock()
                vc = _VC(connected=False)
                player = _Player(vc)
                gs = _GS(player)
                self._seed_refused(handler, gs)
                history_len_before = len(gs.queue._history)
                current_before = gs.queue.current
                queue_before = gs.queue.list()

                sentinel = MagicMock()
                sentinel.done.return_value = False
                gs.auto_next_task = sentinel
                gs.auto_next_gen = 7
                bot = _Bot(gs, _Guild(vc))

                sent = asyncio.run(_invoke(handler, bot, gs))

                sentinel.cancel.assert_not_called()
                self.assertIs(gs.auto_next_task, sentinel, f"{handler}: task must stay armed")
                self.assertEqual(gs.auto_next_gen, 7, f"{handler}: generation must not bump")
                self.assertEqual(player.play_calls, [], f"{handler}: play() must not be attempted")
                self.assertEqual(player.stop_calls, 0, f"{handler}: stop_playback() must not run")
                commands._start_auto_next.assert_not_called()

                if handler == "prev":
                    self.assertEqual(len(gs.queue._history), history_len_before,
                                      "a missing guard would pop history")
                    self.assertIs(gs.queue.current, current_before,
                                   "a missing guard would push current onto the queue")
                elif handler == "skipto":
                    self.assertEqual(len(gs.queue), 3, "a missing guard would drain two into history")
                    self.assertEqual(len(gs.queue._history), 0)
                else:
                    self.assertEqual(gs.queue.list(), queue_before)
                    self.assertIsNone(gs.queue.current)

                self.assertEqual(sum("reconnect" in m.lower() for m in sent), 1,
                                  f"{handler}: expected exactly one reconnect reply, got {sent}")

    def test_voice_healthy_behaves_exactly_as_before(self):
        for handler in self.HANDLERS:
            with self.subTest(handler=handler):
                commands._start_auto_next.reset_mock()
                vc = _VC(connected=True)
                player = _Player(vc)
                gs = _GS(player)
                if handler == "prev":
                    gs.queue._history.append(Track(query="h0", title="h0", requested_by="u"))
                else:
                    gs.queue.add(Track(query="q0", title="q0", requested_by="u"))
                gs.auto_next_gen = 7
                bot = _Bot(gs, _Guild(vc))

                # Exactly one queued track for next/skip/skipto — position=1 keeps
                # skipto's range check valid.
                sent = asyncio.run(_invoke(handler, bot, gs, position=1))

                self.assertEqual(len(player.play_calls), 1, f"{handler}: play() must run")
                self.assertEqual(player.stop_calls, 1, f"{handler}: stop_playback() must run once")
                commands._start_auto_next.assert_called_once()
                self.assertEqual(gs.auto_next_gen, 8, f"{handler}: generation must bump from 7 to 8")
                self.assertFalse(any("reconnect" in m.lower() for m in sent), f"{handler}: {sent}")


class TestManualAdvanceRecovery(unittest.TestCase):
    """Fix 2: the socket can die *during* a manual play() — after the Fix 1 guard
    passed but before the request completed. `_recover_manual_advance` must put the
    popped track back and hand control to the auto-next chain, which knows how to
    wait a reconnect out. Gated on `_live_voice_client(...) is None`, so a genuinely
    dead track with healthy voice is never requeued or re-armed (the infinite-retry
    regression this plan's threat register calls out).
    """

    HANDLERS = ("prev", "next", "skip", "skipto")

    def setUp(self):
        self._patches = [
            patch.object(commands, "check_channel", side_effect=self._true),
            patch.object(commands, "_check_vote", return_value=(True, "")),
            patch.object(commands, "_on_cooldown", return_value=False),
            patch.object(commands, "build_player_view", return_value=None),
            patch.object(commands, "_get_requester_name", return_value="someone"),
            patch.object(commands, "send_new_np", side_effect=self._noop),
            patch.object(commands, "update_np_stopped", side_effect=self._noop),
            patch.object(commands, "_schedule_prefetch", return_value=None),
            patch.object(commands, "_schedule_autoplay_topup", return_value=None),
            patch.object(commands, "_start_auto_next", MagicMock()),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    async def _true(ctx):
        return True

    @staticmethod
    async def _noop(*a, **k):
        return None

    @staticmethod
    def _expected_channel_id(handler):
        return _BUTTON_CHANNEL_ID if handler in ("prev", "next") else _CTX_CHANNEL_ID

    @staticmethod
    def _seed_one(handler, gs):
        """Seed the single track that will get popped by the handler, returning it."""
        if handler == "prev":
            track = Track(query="p", title="p", requested_by="u")
            gs.queue._history.append(track)
            return track
        track = Track(query="n", title="n", requested_by="u")
        gs.queue.add(track)
        return track

    def test_socket_dying_during_play_requeues_and_rearms(self):
        for handler in self.HANDLERS:
            with self.subTest(handler=handler):
                commands._start_auto_next.reset_mock()
                vc = _VC(connected=True)

                def go_down(n, vc=vc):
                    # 1 = the Fix 1 guard, 2 = the except-block re-check
                    if n >= 2:
                        vc._connected = False
                vc.on_check = go_down

                player = _Player(vc, error=RuntimeError("Not connected to a voice channel"))
                gs = _GS(player)
                track = self._seed_one(handler, gs)
                gs.auto_next_gen = 7
                bot = _Bot(gs, _Guild(vc))

                sent = asyncio.run(_invoke(handler, bot, gs, position=1))

                self.assertEqual(len(gs.queue), 1, f"{handler}: track must be back in the queue")
                self.assertIs(gs.queue.list()[0], track, f"{handler}: same track object, head of queue")
                self.assertIsNone(gs.queue.current, f"{handler}: current must be cleared")
                commands._start_auto_next.assert_called_once()
                self.assertEqual(commands._start_auto_next.call_args.args[1],
                                  self._expected_channel_id(handler),
                                  f"{handler}: re-armed on the wrong channel")
                self.assertFalse(any("skipping track" in m.lower() for m in sent),
                                  f"{handler}: false 'skipping' message on a requeue, got {sent}")

    def test_dead_track_with_healthy_voice_does_not_requeue_or_rearm(self):
        from yt_dlp.utils import DownloadError
        for handler in self.HANDLERS:
            with self.subTest(handler=handler):
                commands._start_auto_next.reset_mock()
                vc = _VC(connected=True)
                error = DownloadError("ERROR: [youtube] x: Video unavailable")
                player = _Player(vc, error=error)
                gs = _GS(player)
                track = self._seed_one(handler, gs)
                gs.auto_next_gen = 7
                bot = _Bot(gs, _Guild(vc))

                sent = asyncio.run(_invoke(handler, bot, gs, position=1))

                self.assertNotIn(track, gs.queue.list(),
                                  f"{handler}: a genuinely dead track must not be requeued")
                commands._start_auto_next.assert_not_called()
                self.assertEqual(sum("skipping track" in m.lower() for m in sent), 1,
                                  f"{handler}: {sent}")


class TestVoiceDebugLogging(unittest.TestCase):
    """Fix 3(b): discord.voice_state and discord.player must log at DEBUG so the
    decisive lines from a socket drop are visible; discord.gateway must not, or
    every incident is buried under per-event gateway traffic.
    """

    def test_child_logger_debug_reaches_the_handler_without_flooding_gateway(self):
        """Reproduces discord.py's own setup_logging(): the `discord` logger at
        INFO with a capturing handler at NOTSET. Pins the non-obvious invariant:
        a parent logger's level is not re-checked during propagation, only handler
        levels are — so raising a child logger's own level is sufficient.
        """
        root_discord = logging.getLogger("discord")
        voice_logger = logging.getLogger("discord.voice_state")
        gateway_logger = logging.getLogger("discord.gateway")

        prev_levels = (root_discord.level, voice_logger.level, gateway_logger.level)

        def _restore():
            root_discord.setLevel(prev_levels[0])
            voice_logger.setLevel(prev_levels[1])
            gateway_logger.setLevel(prev_levels[2])
        self.addCleanup(_restore)

        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Capture()
        handler.setLevel(logging.NOTSET)
        root_discord.addHandler(handler)
        self.addCleanup(root_discord.removeHandler, handler)

        root_discord.setLevel(logging.INFO)
        voice_logger.setLevel(logging.DEBUG)
        # discord.gateway is left alone — its effective level is inherited INFO.

        voice_logger.debug("voice debug line")
        gateway_logger.debug("gateway debug line — must not appear")

        messages = [r.getMessage() for r in records]
        self.assertIn("voice debug line", messages)
        self.assertNotIn("gateway debug line — must not appear", messages)

    def test_main_raises_the_two_voice_loggers_before_run(self):
        """Read main.py as text rather than importing it — importing spawns the
        bgutil subprocess."""
        main_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
        with open(main_path, "r", encoding="utf-8") as f:
            src = f.read()

        voice_state_line = 'logging.getLogger("discord.voice_state").setLevel(logging.DEBUG)'
        player_line = 'logging.getLogger("discord.player").setLevel(logging.DEBUG)'
        run_call = "bot.run(token)"

        self.assertIn(voice_state_line, src)
        self.assertIn(player_line, src)
        self.assertIn(run_call, src)

        self.assertLess(src.index(voice_state_line), src.index(run_call),
                         "discord.voice_state DEBUG must be set before bot.run()")
        self.assertLess(src.index(player_line), src.index(run_call),
                         "discord.player DEBUG must be set before bot.run()")

        self.assertNotIn('logging.getLogger("discord").setLevel(logging.DEBUG)', src,
                          "global discord DEBUG would flood the log with gateway traffic")


if __name__ == "__main__":
    unittest.main(verbosity=2)
