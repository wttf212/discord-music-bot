"""Tests for surviving a voice-socket drop (WebSocket 1006) during auto-next.

A dead voice socket used to be misclassified as a retryable *track* error: play()
raised RuntimeError("Not connected to a voice channel"), which the auto-next loop
read as "bad track", burning three queued songs and tripping the circuit breaker
while discord.py was still reconnecting in the background. These tests cover the
requeue-and-wait fix plus the AudioPlayer.disconnect() leak it depends on.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import patch

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

    def __init__(self):
        self.members = [_Member(bot=False)]


class _VC:
    """Fake discord.py VoiceClient tracking connection state across polls."""

    def __init__(self, connected: bool, flip_after: int | None = None, on_check=None):
        self._connected = connected
        self.flip_after = flip_after
        self.on_check = on_check
        self.check_count = 0
        self.channel = _VoiceChannel()

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
        pass


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
