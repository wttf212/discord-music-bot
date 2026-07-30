"""Tests for warm YoutubeDL reuse in get_audio_url().

A fresh instance per resolve discards yt-dlp's player-JS/solver warm-up (measured
2.54s fresh vs 2.09s warm, t=-7.17). These pin the reuse and, more importantly, the
safety rule: an instance is never handed to two threads at once.
"""
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_player
from audio_player import _resolver, _RESOLVER_MAX


class FakeYDL:
    instances = []

    def __init__(self, opts):
        self.opts = opts
        self.closed = False
        FakeYDL.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.closed = True


class ResolverTestCase(unittest.TestCase):
    def setUp(self):
        audio_player._resolvers.clear()
        FakeYDL.instances = []
        self._patcher = patch.object(audio_player, "YoutubeDL", FakeYDL)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        audio_player._resolvers.clear()


class TestWarmReuse(ResolverTestCase):

    def test_same_key_reuses_one_instance(self):
        key = ("web_embedded", None, False, False, True)
        with _resolver(key, {}) as a:
            pass
        with _resolver(key, {}) as b:
            pass
        self.assertIs(a, b)
        self.assertEqual(len(FakeYDL.instances), 1)

    def test_pooled_instance_is_not_closed(self):
        key = ("web_embedded", None, False, False, True)
        with _resolver(key, {}) as ydl:
            pass
        self.assertFalse(ydl.closed, "a warm instance must survive for the next resolve")

    def test_different_keys_get_different_instances(self):
        with _resolver(("web_embedded", None, False, False, True), {}) as a:
            pass
        with _resolver(("web_embedded", None, True, False, True), {}) as b:
            pass
        self.assertIsNot(a, b)
        self.assertEqual(len(audio_player._resolvers), 2)

    def test_force_cli_key_differs_so_no_reentrancy(self):
        """The degraded-resolve retry recurses; it must not re-enter a held lock."""
        outer = ("web_embedded", None, False, False, True)
        inner = ("web_embedded", None, False, True, True)
        self.assertNotEqual(outer, inner)
        with _resolver(outer, {}) as a:
            with _resolver(inner, {}) as b:   # would deadlock if the key were shared
                self.assertIsNot(a, b)

    def test_pool_is_capped(self):
        for i in range(_RESOLVER_MAX + 3):
            with _resolver(("client%d" % i, None, False, False, True), {}):
                pass
        self.assertEqual(len(audio_player._resolvers), _RESOLVER_MAX)

    def test_overflow_key_still_works_with_throwaway(self):
        for i in range(_RESOLVER_MAX):
            with _resolver(("client%d" % i, None, False, False, True), {}):
                pass
        with _resolver(("overflow", None, False, False, True), {}) as ydl:
            self.assertIsInstance(ydl, FakeYDL)
        self.assertTrue(ydl.closed, "an unpooled throwaway must be closed")


class TestConcurrencySafety(ResolverTestCase):

    def test_busy_instance_is_never_shared(self):
        key = ("web_embedded", None, False, False, True)
        seen = []
        started = threading.Event()
        release = threading.Event()

        def hold():
            with _resolver(key, {}) as ydl:
                seen.append(ydl)
                started.set()
                release.wait(5)

        t = threading.Thread(target=hold)
        t.start()
        started.wait(5)
        with _resolver(key, {}) as second:
            seen.append(second)
        release.set()
        t.join(5)

        self.assertIsNot(seen[0], seen[1],
                         "a busy instance must not be handed to a second thread")
        self.assertTrue(seen[1].closed, "the contended caller gets a closed throwaway")

    def test_contention_never_blocks(self):
        """No guild may queue behind another guild's ~2s resolve."""
        key = ("web_embedded", None, False, False, True)
        started, release = threading.Event(), threading.Event()

        def hold():
            with _resolver(key, {}):
                started.set()
                release.wait(5)

        t = threading.Thread(target=hold)
        t.start()
        started.wait(5)
        t0 = time.perf_counter()
        with _resolver(key, {}):
            pass
        elapsed = time.perf_counter() - t0
        release.set()
        t.join(5)
        self.assertLess(elapsed, 0.5, f"contended acquire blocked for {elapsed:.2f}s")

    def test_lock_released_when_body_raises(self):
        key = ("web_embedded", None, False, False, True)
        with self.assertRaises(ValueError):
            with _resolver(key, {}):
                raise ValueError("boom")
        # If the lock leaked, this would hand out a throwaway instead of the warm one.
        with _resolver(key, {}) as ydl:
            pass
        self.assertFalse(ydl.closed, "lock leaked: warm instance no longer reachable")


class TestGetAudioUrlUsesResolver(unittest.TestCase):

    def test_extraction_goes_through_resolver(self):
        import inspect
        src = inspect.getsource(audio_player._resolve_audio_url)
        self.assertIn("_resolver(resolver_key, ydl_opts)", src)
        self.assertNotIn("with YoutubeDL(ydl_opts) as ydl", src,
                         "extraction must not build its own instance any more")

    def test_resolver_key_covers_every_option_that_shapes_opts(self):
        """A key collision would hand back an instance configured for other options."""
        import inspect
        src = inspect.getsource(audio_player._resolve_audio_url)
        self.assertIn("resolver_key = (client, cookies_file, debug, force_cli, is_yt)", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
