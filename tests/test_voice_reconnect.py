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


if __name__ == "__main__":
    unittest.main(verbosity=2)
