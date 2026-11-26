"""Tests for a playback start that a stop or a newer start overtakes part-way.

play() awaits the lookup, the stream open, the FFmpeg spawn and the first frame, and a stop
or a newer play() can land in any of those waits. Until the guard after the gap speech, the
overtaken start used to carry on with state it shares with the newer one:

* its stream handle replaced the newer start's in ``_ytdlp_proc``, so the newer track
  played on untracked and a later stop never reaped its reader;
* a first frame emptied by the stop took the stale-URL branch: a second lookup, a strike
  against the fast client, a second stream open;
* its track overwrote the newer start's.

A stop during the FFmpeg spawn is in test_stop_during_spawn.py.
"""
import asyncio
import contextlib
import os
import sys
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
import audio_player
from test_play_abort import INFO, _FakeFFmpeg, _FakeVoice, _Harness
from test_stream_reader import _wait_until

INFO_A = dict(INFO, title="A")
INFO_B = dict(INFO, title="B")


class _ParkOnce(_Harness):
    """Parks only the first start to reach the stage: the newer start goes straight through."""

    def park(self, stage):
        if stage == self.stop_at and not self.entered.is_set():
            super().park(stage)


class _ReapAwareFFmpeg(_FakeFFmpeg):
    """A first read that comes back empty once its stream reader has been reaped, as FFmpeg's
    does when the stop terminated the reader under it."""

    def __init__(self, harness, stdout):
        super().__init__(harness)
        self._proc = next(p for p in harness.procs if p.stdout is stdout)

    def read(self):
        frame = super().read()
        return b"" if self._proc.terminated.is_set() else frame


class _ReapingHarness(_ParkOnce):
    @contextlib.contextmanager
    def patched(self):
        with super().patched(), \
                patch.object(discord, "FFmpegPCMAudio",
                             lambda stdout, **kwargs: _ReapAwareFFmpeg(self, stdout)):
            yield


def _player(voice):
    with patch.object(audio_player, "_find_ffmpeg", return_value="ffmpeg"):
        player = audio_player.AudioPlayer({"audio": {"sample_rate": 48000, "channels": 2},
                                           "youtube": {"client": "web"}})
    player._voice_client = voice
    return player


async def _wait_parked(harness):
    parked = await asyncio.get_running_loop().run_in_executor(None, harness.entered.wait, 5)
    assert parked, f"play() never reached {harness.stop_at!r}"


async def _outcome(task):
    try:
        return await asyncio.wait_for(task, 5)
    except BaseException as e:   # CancelledError included: a start that gave up
        return e


class _Base(unittest.TestCase):

    def run_scenario(self, harness, scenario, player=None):
        player = player or _player(_FakeVoice())
        # The jitter buffers of a start that did play keep a fill thread until cleaned up.
        self.addCleanup(lambda: [b.cleanup() for b in harness.buffers])
        with harness.patched():
            result = asyncio.run(scenario(player))
        return player, result

    def assert_reaped(self, proc, what="stream reader"):
        self.assertTrue(_wait_until(proc.terminated.is_set), f"{what} left running")


class TestOvertakenWhileOpeningTheStream(_Base):

    def test_a_newer_start_keeps_its_stream_handle(self):
        """A's open returns after B has finished starting. A used to put its own reader in
        _ytdlp_proc and then clear it, so B played on with nothing tracking its reader."""
        harness = _ParkOnce(stop_at="open")

        async def scenario(player):
            first = asyncio.ensure_future(player.play("a", prefetched_info=dict(INFO_A)))
            await _wait_parked(harness)
            await player.play("b", prefetched_info=dict(INFO_B))
            harness.release.set()
            return await _outcome(first)

        player, outcome = self.run_scenario(harness, scenario)
        self.assertIsInstance(outcome, asyncio.CancelledError, outcome)
        stream_a, stream_b = harness.procs
        self.assertIs(player._ytdlp_proc, stream_b, "the newer start's reader is not tracked")
        self.assertFalse(stream_b.terminated.is_set())
        self.assert_reaped(stream_a, "the overtaken start's reader")
        player.stop_playback()
        self.assertTrue(stream_b.terminated.is_set(), "a stop never reached the newer reader")
        self.assertEqual(len(harness.ffmpegs), 1, "the overtaken start went on to build FFmpeg")

    def test_a_stop_ends_the_start_before_it_builds_anything_more(self):
        harness = _ParkOnce(stop_at="open")

        async def scenario(player):
            start = asyncio.ensure_future(player.play("a", prefetched_info=dict(INFO_A)))
            await _wait_parked(harness)
            player.stop_playback()
            harness.release.set()
            return await _outcome(start)

        player, outcome = self.run_scenario(harness, scenario)
        self.assertIsInstance(outcome, asyncio.CancelledError, outcome)
        self.assertEqual(len(harness.procs), 1)
        self.assert_reaped(harness.procs[0])
        self.assertEqual(harness.ffmpegs, [], "the stopped start still built FFmpeg")
        self.assertIsNone(player._ytdlp_proc)


class _RefusingHarness(_ParkOnce):
    """The first stream open is refused (a fast-client URL answering 403) once released."""

    def open_stream(self, *args):
        self.park("open")
        raise audio_player._StreamRefusedError("HTTP 403 at offset 0")


class TestOvertakenWhileTheStreamIsRefused(_Base):

    def test_a_stop_then_a_refusal_does_not_start_another_lookup(self):
        """A refused fast-client URL is retried with a fresh lookup by the configured
        client: a YouTube request nobody is waiting for once the start was stopped."""
        harness = _RefusingHarness(stop_at="open")

        async def scenario(player):
            start = asyncio.ensure_future(player.play("a", prefetched_info=dict(INFO_A)))
            await _wait_parked(harness)
            player.stop_playback()
            harness.release.set()
            return await _outcome(start)

        with patch.object(audio_player, "resolve_audio",
                          AsyncMock(return_value=dict(INFO))) as resolve:
            player, outcome = self.run_scenario(harness, scenario)
        self.assertIsInstance(outcome, asyncio.CancelledError, outcome)
        resolve.assert_not_called()
        self.assertEqual(harness.ffmpegs, [])


class TestOvertakenWhileLookingUp(_Base):

    def test_a_stop_during_the_lookup_opens_no_stream(self):
        """The lookup is a YouTube request already made; opening the stream on its result
        is a second one, and the settle ladder behind it can run for many seconds."""
        harness = _Harness()
        entered, release = threading.Event(), asyncio.Event()

        async def slow_resolve(*args, **kwargs):
            entered.set()
            await release.wait()
            return dict(INFO_A)

        async def scenario(player):
            start = asyncio.ensure_future(player.play("a"))
            await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)
            player.stop_playback()
            release.set()
            return await _outcome(start)

        with patch.object(audio_player, "resolve_audio", slow_resolve):
            player, outcome = self.run_scenario(harness, scenario)
        self.assertIsInstance(outcome, asyncio.CancelledError, outcome)
        self.assertEqual(harness.procs, [], "a stream was opened for a stopped start")


class TestOvertakenInTheFirstFrameWindow(_Base):
    """The stop (or B's stop_playback(keep_speech=True)) reaps A's reader, so A's first frame
    comes back empty. That is the stop's doing, not a stale URL."""

    def _lookups(self):
        return (patch.object(audio_player, "resolve_audio", AsyncMock(return_value=dict(INFO))),
                patch.object(audio_player, "invalidate_resolve_cache", Mock()))

    def test_a_stop_costs_no_second_lookup_and_no_second_stream(self):
        harness = _ReapingHarness(stop_at="first_frame")

        async def scenario(player):
            start = asyncio.ensure_future(player.play("a", prefetched_info=dict(INFO_A)))
            await _wait_parked(harness)
            player.stop_playback()
            harness.release.set()
            return await _outcome(start)

        resolve, invalidate = self._lookups()
        with resolve as resolve_mock, invalidate as invalidate_mock:
            player, outcome = self.run_scenario(harness, scenario)
        self.assertIsInstance(outcome, asyncio.CancelledError, outcome)
        resolve_mock.assert_not_called()
        invalidate_mock.assert_not_called()
        self.assertEqual(len(harness.procs), 1, "a second stream was opened after the stop")
        self.assert_reaped(harness.procs[0])

    def test_a_stop_is_no_strike_against_the_fast_client(self):
        """Two strikes in the last five bench the fast client for 30 minutes, for every guild."""
        harness = _ReapingHarness(stop_at="first_frame")
        fast_info = dict(INFO_A, client="fast-c", minted_at=time.time())

        async def scenario(player):
            start = asyncio.ensure_future(player.play("a", prefetched_info=dict(fast_info)))
            await _wait_parked(harness)
            player.stop_playback()
            harness.release.set()
            return await _outcome(start)

        resolve, invalidate = self._lookups()
        with resolve as resolve_mock, invalidate, \
                patch.object(audio_player, "_FAST_CLIENT_C", "fast-c"), \
                patch.object(audio_player, "_note_fast_stream_refused") as strike, \
                patch.object(audio_player, "_note_fast_attempt") as attempt:
            player, outcome = self.run_scenario(harness, scenario)
        self.assertIsInstance(outcome, asyncio.CancelledError, outcome)
        strike.assert_not_called()
        attempt.assert_not_called()
        resolve_mock.assert_not_called()
        self.assertEqual(len(harness.procs), 1)

    def test_a_newer_start_keeps_its_stream_and_state(self):
        """B's stop reaps A's reader, A's first frame comes back empty or not, and A must
        leave B's handle, B's track and B's done-flag alone either way."""
        for reaped in (True, False):
            with self.subTest(first_frame_empty=reaped):
                harness = (_ReapingHarness if reaped else _ParkOnce)(stop_at="first_frame")

                async def scenario(player):
                    first = asyncio.ensure_future(
                        player.play("a", prefetched_info=dict(INFO_A)))
                    await _wait_parked(harness)
                    await player.play("b", prefetched_info=dict(INFO_B))
                    harness.release.set()
                    return await _outcome(first)

                resolve, invalidate = self._lookups()
                with resolve as resolve_mock, invalidate as invalidate_mock:
                    player, outcome = self.run_scenario(harness, scenario)
                self.assertIsInstance(outcome, asyncio.CancelledError, outcome)
                resolve_mock.assert_not_called()
                invalidate_mock.assert_not_called()
                stream_a, stream_b = harness.procs
                self.assertIs(player._ytdlp_proc, stream_b)
                self.assertFalse(stream_b.terminated.is_set())
                self.assertEqual(player.current_track_title, "B")
                self.assertTrue(player.is_playing)
                self.assertFalse(player._playback_done.is_set())
                player.stop_playback()
                self.assertTrue(stream_b.terminated.is_set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
