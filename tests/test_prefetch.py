"""Tests for the fair-play-aware next-track prefetch and its anti-ban throttles.

Verify that _prefetch_next_track:
  * resolves the fair-play-predicted next track and caches it on the Track,
  * skips a track that already holds a fresh cached resolve,
  * DEFERS (never drops) when the global minimum interval has not elapsed,
  * never prefetches a live radio track,
and that _schedule_prefetch keeps at most one prefetch in flight per guild.
"""
import asyncio
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import commands
from track_queue import TrackQueue, Track


def _fresh_info():
    return {
        "url": f"https://r.googlevideo.com/videoplayback?expire={int(time.time()) + 6 * 3600}",
        "title": "Resolved Song",
        "thumbnail": "",
        "webpage_url": "",
    }


def _stale_info():
    return {
        "url": f"https://r.googlevideo.com/videoplayback?expire={int(time.time()) - 60}",
        "title": "Old Song",
        "thumbnail": "",
        "webpage_url": "",
    }


class _GS:
    def __init__(self):
        self.queue = TrackQueue()
        self.prefetch_task = None


class _Bot:
    def __init__(self):
        self.config = {"youtube": {"client": "web"}}
        self._gs = _GS()

    def get_guild_state(self, guild_id):
        return self._gs


class TestPrefetch(unittest.TestCase):
    def setUp(self):
        commands._last_prefetch_monotonic = 0.0
        # The fake resolves below carry *.googlevideo.com URLs, so the post-resolve
        # CDN warm-up would treat them as real and probe the network. Stub it out —
        # warming has its own tests in test_stream_warmup.py.
        self._warm = patch.object(commands, "warm_stream_url", return_value=True)
        self._warm.start()
        self.addCleanup(self._warm.stop)

    def _run(self, coro):
        return asyncio.run(coro)

    def test_resolves_and_caches_next_track(self):
        bot = _Bot()
        bot._gs.queue.add(Track(query="q1", title="q1", requested_by="u1"))
        bot._gs.queue.add(Track(query="q2", title="q2", requested_by="u2"))
        with patch.object(commands, "get_audio_url_with_retry", return_value=_fresh_info()) as m:
            self._run(commands._prefetch_next_track(bot, 1))
        nxt = bot._gs.queue.list()[0]
        self.assertIsNotNone(nxt.resolved_info)
        self.assertGreater(nxt.resolved_at, 0)
        self.assertEqual(m.call_count, 1)
        # It resolved the fair-play-predicted next track (query q1), not q2.
        self.assertEqual(m.call_args[0][0], "q1")

    def test_skips_when_already_fresh(self):
        bot = _Bot()
        t = Track(query="q1", title="q1", requested_by="u1")
        t.resolved_info = _fresh_info()
        t.resolved_at = time.time()
        bot._gs.queue.add(t)
        with patch.object(commands, "get_audio_url_with_retry", return_value=_fresh_info()) as m:
            self._run(commands._prefetch_next_track(bot, 1))
        self.assertEqual(m.call_count, 0)  # already fresh — no refetch

    def test_global_throttle_defers_instead_of_dropping(self):
        """The floor must DELAY the second resolve, not discard it.

        Dropping it was the skip-lag bug: nothing rescheduled, so the next track
        resolved at play time (~2s) and could hit the CDN settle window on top.
        Skipping every ~10s against an 8s floor lost roughly every other prefetch.
        """
        bot = _Bot()
        bot._gs.queue.add(Track(query="q1", title="q1", requested_by="u1"))
        bot._gs.queue.add(Track(query="q2", title="q2", requested_by="u1"))
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        # Stale results so skip-if-fresh never triggers — isolates the throttle.
        with patch.object(commands, "get_audio_url_with_retry", return_value=_stale_info()) as m, \
             patch("asyncio.sleep", fake_sleep):
            self._run(commands._prefetch_next_track(bot, 1))
            self._run(commands._prefetch_next_track(bot, 1))
        self.assertEqual(m.call_count, 2, "the deferred prefetch must still happen")
        self.assertEqual(len(slept), 1, "only the throttled call should wait")
        self.assertGreater(slept[0], 0)
        self.assertLessEqual(slept[0], commands._PREFETCH_MIN_INTERVAL,
                             "never wait longer than the floor itself")

    def test_spacing_floor_still_respected(self):
        """Anti-ban invariant: resolves stay >= _PREFETCH_MIN_INTERVAL apart."""
        bot = _Bot()
        bot._gs.queue.add(Track(query="q1", title="q1", requested_by="u1"))
        commands._last_prefetch_monotonic = time.monotonic()
        waited = []

        async def fake_sleep(seconds):
            waited.append(seconds)

        with patch.object(commands, "get_audio_url_with_retry", return_value=_stale_info()), \
             patch("asyncio.sleep", fake_sleep):
            self._run(commands._prefetch_next_track(bot, 1))
        self.assertTrue(waited and waited[0] > 7.0,
                        f"expected a ~8s deferral, waited {waited}")

    def test_requeries_next_track_after_deferring(self):
        """A skip during the wait changes what plays next — re-read, don't prefetch
        the track that was next when the prefetch was scheduled."""
        bot = _Bot()
        bot._gs.queue.add(Track(query="stale-pick", title="s", requested_by="u1"))
        commands._last_prefetch_monotonic = time.monotonic()  # force a deferral

        async def fake_sleep(seconds):
            bot._gs.queue.clear()
            bot._gs.queue.add(Track(query="actual-next", title="a", requested_by="u1"))

        with patch.object(commands, "get_audio_url_with_retry", return_value=_stale_info()) as m, \
             patch("asyncio.sleep", fake_sleep):
            self._run(commands._prefetch_next_track(bot, 1))
        self.assertEqual(m.call_args[0][0], "actual-next")

    def test_radio_track_not_prefetched(self):
        bot = _Bot()
        bot._gs.queue.add(Track(query="stn", title="Station", requested_by="u1", is_radio=True))
        with patch.object(commands, "get_audio_url_with_retry", return_value=_fresh_info()) as m:
            self._run(commands._prefetch_next_track(bot, 1))
        self.assertEqual(m.call_count, 0)

    def test_empty_queue_noop(self):
        bot = _Bot()
        with patch.object(commands, "get_audio_url_with_retry", return_value=_fresh_info()) as m:
            self._run(commands._prefetch_next_track(bot, 1))
        self.assertEqual(m.call_count, 0)

    def test_schedule_prefetch_single_in_flight(self):
        bot = _Bot()
        running = MagicMock()
        running.done.return_value = False
        bot._gs.prefetch_task = running
        commands._schedule_prefetch(bot, 1)
        # A prefetch is already running — the slot must be untouched (no second task).
        self.assertIs(bot._gs.prefetch_task, running)


if __name__ == "__main__":
    unittest.main(verbosity=2)
