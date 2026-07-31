"""Tests for dropping permanently-unplayable tracks during prefetch.

"Video unavailable. This video has been removed by the uploader" resolves in ~0.5s and
is correctly non-retryable, so the failure itself is cheap. The cost was structural: the
prefetch spent its cycle on the dead track, so when playback reached it the user paid a
failed play AND a cold start (~2s resolve + ~2.8s CDN settle) for whatever followed.
Dropping it during prefetch means the next real track gets resolved and warmed instead.
"""
import asyncio
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_player
import commands
from audio_player import is_permanent_resolve_error
from track_queue import TrackQueue, Track
from yt_dlp.utils import DownloadError

REMOVED = DownloadError(
    "ERROR: [youtube] DSa3ttNsI9U: Video unavailable. "
    "This video has been removed by the uploader")
RATE_LIMITED = DownloadError("ERROR: [youtube] HTTP Error 429: Too Many Requests")


def _fresh_info(title="Resolved"):
    return {
        "url": f"https://r.googlevideo.com/videoplayback?expire={int(time.time()) + 6 * 3600}",
        "title": title, "thumbnail": "", "webpage_url": "",
        "is_audio_only": True, "protocol": "https", "is_live": False,
    }


class _GS:
    def __init__(self):
        self.queue = TrackQueue()
        self.prefetch_task = None


class _Bot:
    config = {"youtube": {"client": "web"}, "debug": False}

    def __init__(self):
        self._gs = _GS()

    def get_guild_state(self, guild_id):
        return self._gs


class TestPermanentErrorClassification(unittest.TestCase):

    def test_removed_by_uploader_is_permanent(self):
        self.assertTrue(is_permanent_resolve_error(REMOVED))

    def test_rate_limit_is_not_permanent(self):
        self.assertFalse(is_permanent_resolve_error(RATE_LIMITED))

    def test_connection_error_is_not_permanent(self):
        self.assertFalse(is_permanent_resolve_error(ConnectionError("reset")))

    def test_unknown_error_is_not_permanent(self):
        """Ambiguous errors must stay retryable — never drop a track on a guess."""
        self.assertFalse(is_permanent_resolve_error(DownloadError("ERROR: something odd")))


class TestQueueDiscard(unittest.TestCase):

    def test_discard_removes_by_identity(self):
        q = TrackQueue()
        a = Track(query="a", title="a", requested_by="u")
        b = Track(query="b", title="b", requested_by="u")
        q.add(a); q.add(b)
        self.assertTrue(q.discard(a))
        self.assertEqual([t.query for t in q.list()], ["b"])

    def test_discard_of_absent_track_is_false(self):
        q = TrackQueue()
        q.add(Track(query="a", title="a", requested_by="u"))
        self.assertFalse(q.discard(Track(query="ghost", title="g", requested_by="u")))

    def test_discard_targets_the_right_duplicate(self):
        """Two tracks can share a query — identity must decide, not equality."""
        q = TrackQueue()
        a = Track(query="same", title="first", requested_by="u")
        b = Track(query="same", title="second", requested_by="u")
        q.add(a); q.add(b)
        q.discard(b)
        self.assertEqual([t.title for t in q.list()], ["first"])


class TestPrefetchDropsDeadTracks(unittest.TestCase):

    def setUp(self):
        commands._last_prefetch_monotonic = 0.0
        self._warm = patch.object(commands, "warm_stream_url", return_value=True)
        self._warm.start()
        self.addCleanup(self._warm.stop)

    def test_dead_track_dropped_and_next_one_prefetched(self):
        bot = _Bot()
        dead = Track(query="dead", title="Dead Song", requested_by="u1")
        good = Track(query="good", title="Good Song", requested_by="u1")
        bot._gs.queue.add(dead)
        bot._gs.queue.add(good)

        def resolve(query, *a, **k):
            if query == "dead":
                raise REMOVED
            return _fresh_info("Good Song")

        with patch.object(commands, "get_audio_url_with_retry", side_effect=resolve):
            asyncio.run(commands._prefetch_next_track(bot, 1))

        self.assertEqual([t.query for t in bot._gs.queue.list()], ["good"],
                         "the unplayable track must be gone from the queue")
        self.assertIsNotNone(good.resolved_info,
                             "the track behind it must be prefetched in the same pass")

    def test_transient_error_does_not_drop_the_track(self):
        """A 429 is not the track's fault — play() must still get its chance."""
        bot = _Bot()
        t = Track(query="q", title="Song", requested_by="u1")
        bot._gs.queue.add(t)
        with patch.object(commands, "get_audio_url_with_retry", side_effect=RATE_LIMITED):
            asyncio.run(commands._prefetch_next_track(bot, 1))
        self.assertEqual(len(bot._gs.queue.list()), 1)
        self.assertIsNone(t.resolved_info)

    def test_run_of_dead_tracks_is_bounded(self):
        """A playlist of removed videos must not spin the prefetch forever."""
        bot = _Bot()
        for i in range(10):
            bot._gs.queue.add(Track(query=f"dead{i}", title=f"d{i}", requested_by="u1"))
        calls = []

        def resolve(query, *a, **k):
            calls.append(query)
            raise REMOVED

        with patch.object(commands, "get_audio_url_with_retry", side_effect=resolve):
            asyncio.run(commands._prefetch_next_track(bot, 1))

        self.assertEqual(len(calls), commands._PREFETCH_MAX_DEAD_SKIPS + 1)
        self.assertEqual(len(bot._gs.queue.list()),
                         10 - (commands._PREFETCH_MAX_DEAD_SKIPS + 1))

    def test_all_dead_then_empty_queue_is_handled(self):
        bot = _Bot()
        bot._gs.queue.add(Track(query="dead", title="d", requested_by="u1"))
        with patch.object(commands, "get_audio_url_with_retry", side_effect=REMOVED):
            asyncio.run(commands._prefetch_next_track(bot, 1))
        self.assertEqual(len(bot._gs.queue.list()), 0)

    def test_healthy_track_still_prefetches_normally(self):
        bot = _Bot()
        t = Track(query="q", title="Song", requested_by="u1")
        bot._gs.queue.add(t)
        with patch.object(commands, "get_audio_url_with_retry",
                          return_value=_fresh_info()) as m:
            asyncio.run(commands._prefetch_next_track(bot, 1))
        self.assertEqual(m.call_count, 1)
        self.assertIsNotNone(t.resolved_info)

class TestCircuitBreakerScoping(unittest.TestCase):
    """Unavailable tracks must not trip the systemic-failure breaker."""

    def test_auto_next_excludes_permanent_errors_from_the_counter(self):
        import inspect
        src = inspect.getsource(commands._auto_next)
        marker = src.index("is_permanent_resolve_error(e)")
        counter = src.index("consecutive_errors += 1")
        self.assertLess(marker, counter,
                        "permanent per-track errors must be handled before the counter "
                        "increments, or a few dead songs end the session")

    def test_breaker_still_exists_for_systemic_failure(self):
        import inspect
        src = inspect.getsource(commands._auto_next)
        self.assertIn("MAX_CONSECUTIVE_ERRORS", src)
        self.assertIn("stopping auto-play", src)

if __name__ == "__main__":
    unittest.main(verbosity=2)
