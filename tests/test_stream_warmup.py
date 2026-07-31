"""Tests for warming a freshly resolved CDN URL off the critical path.

A googlevideo URL 403s for ~2.5s after it is resolved. Paying that during the previous
track's playback turns a skip from ~2.8s of retries into a 0.02s first byte. Probes use
a 1-byte range and do not consume the URL (verified live before this was built).
"""
import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_player
from audio_player import warm_stream_url

URL = "https://r1---sn-x.googlevideo.com/videoplayback?id=abc&clen=1024"


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.closed = False

    def iter_content(self, chunk_size=1):
        yield b"\x00"

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, statuses):
        self._statuses = list(statuses)
        self.requests = []
        self.closed = False

    def get(self, url, **kwargs):
        self.requests.append(url)
        if not self._statuses:
            return FakeResponse(403)
        nxt = self._statuses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return FakeResponse(nxt)

    def close(self):
        self.closed = True


def _warm(statuses, url=URL, debug=False):
    """Run warm_stream_url against a fake curl_cffi session."""
    session = FakeSession(statuses)
    fake_requests = types.ModuleType("curl_cffi.requests")
    fake_requests.Session = lambda **kw: session
    fake_root = types.ModuleType("curl_cffi")
    fake_root.requests = fake_requests
    with patch.dict(sys.modules, {"curl_cffi": fake_root,
                                  "curl_cffi.requests": fake_requests}), \
         patch.object(audio_player, "_IMPERSONATE_AVAILABLE", True), \
         patch.object(audio_player.time, "sleep", lambda s: None):
        return warm_stream_url(url, debug=debug), session


class TestWarmStreamUrl(unittest.TestCase):

    def test_immediate_200_needs_one_probe(self):
        ok, session = _warm([200])
        self.assertTrue(ok)
        self.assertEqual(len(session.requests), 1)

    def test_206_also_counts_as_live(self):
        ok, _ = _warm([206])
        self.assertTrue(ok)

    def test_retries_through_the_settle_window(self):
        """The real distribution: 403 until ~2.5s, then live."""
        ok, session = _warm([403, 403, 200])
        self.assertTrue(ok)
        self.assertEqual(len(session.requests), 3)

    def test_probe_uses_a_single_byte_range(self):
        """A probe must transfer nothing — it is not a download."""
        _, session = _warm([200])
        self.assertTrue(session.requests[0].endswith("&range=0-0"),
                        f"expected a 1-byte range, got {session.requests[0]}")

    def test_exhausted_ladder_returns_false_not_an_error(self):
        """Failing to warm is harmless — playback waits the window out itself."""
        ok, session = _warm([403] * 40)
        self.assertFalse(ok)
        self.assertEqual(len(session.requests), len(audio_player._STREAM_SETTLE_BACKOFF) + 1)

    def test_bounded_by_the_settle_ladder(self):
        """Never probe more times than the production ladder allows."""
        _, session = _warm([403] * 40)
        self.assertLessEqual(len(session.requests), 12)

    def test_connection_error_is_retried_not_fatal(self):
        ok, session = _warm([ConnectionError("reset"), 200])
        self.assertTrue(ok)
        self.assertEqual(len(session.requests), 2)

    def test_session_always_closed(self):
        _, session = _warm([403, 403, 200])
        self.assertTrue(session.closed)

    def test_noop_without_impersonation(self):
        """No curl_cffi means no browser TLS — never probe with a bare client."""
        with patch.object(audio_player, "_IMPERSONATE_AVAILABLE", False):
            self.assertFalse(warm_stream_url(URL))

    def test_noop_on_empty_url(self):
        with patch.object(audio_player, "_IMPERSONATE_AVAILABLE", True):
            self.assertFalse(warm_stream_url(""))


class TestPrefetchWarmsOnlyWhatItShould(unittest.TestCase):

    def test_prefetch_warms_after_resolving(self):
        import inspect
        import commands
        src = inspect.getsource(commands._prefetch_next_track)
        self.assertIn("warm_stream_url", src)
        self.assertIn("_can_stream_in_process(info)", src,
                      "only googlevideo progressive URLs should be warmed — the "
                      "subprocess path has its own retry handling")

    def test_warm_failure_keeps_the_resolve(self):
        """If warming blows up, the prefetch's resolve must survive — playback then
        just waits the settle window out itself, as it did before warming existed."""
        import asyncio
        import time as _time
        import commands
        from track_queue import TrackQueue, Track

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

        info = {
            "url": "https://r1---sn-x.googlevideo.com/videoplayback"
                   f"?expire={int(_time.time()) + 6 * 3600}&id=abc",
            "title": "Song", "is_audio_only": True, "protocol": "https",
            "is_live": False, "thumbnail": "", "webpage_url": "",
        }
        bot = _Bot()
        bot._gs.queue.add(Track(query="q1", title="q1", requested_by="u1"))
        commands._last_prefetch_monotonic = 0.0

        def boom(*a, **k):
            raise RuntimeError("probe exploded")

        with patch.object(commands, "get_audio_url_with_retry", return_value=info), \
             patch.object(commands, "warm_stream_url", boom):
            asyncio.run(commands._prefetch_next_track(bot, 1))

        nxt = bot._gs.queue.list()[0]
        self.assertIsNotNone(nxt.resolved_info, "the resolve must survive a warm failure")
        self.assertEqual(nxt.resolved_info["title"], "Song")


if __name__ == "__main__":
    unittest.main(verbosity=2)
