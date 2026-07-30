"""Tests for in-flight resolve de-duplication.

A skip fires play()'s resolve while the background prefetch for that same track may
still be running. Both used to issue their own /player call — double the API traffic in
exactly the skip-heavy pattern where rate limiting matters most, and the loser also got
a cold throwaway resolver instance. The second caller now waits for the first.
"""
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_player
from audio_player import get_audio_url, _claim_resolve, _release_resolve

VID = "abc12345678"
WATCH = f"https://www.youtube.com/watch?v={VID}"


def _info(video_id=VID, title="Song"):
    return {
        "url": f"https://r.googlevideo.com/videoplayback?expire={int(time.time()) + 6 * 3600}&id={video_id}",
        "title": title, "thumbnail": "", "webpage_url": f"https://youtu.be/{video_id}",
        "is_audio_only": True, "duration": 1, "artist": "",
        "protocol": "https", "is_live": False,
    }


class InflightTestCase(unittest.TestCase):
    def setUp(self):
        audio_player._resolve_cache.clear()
        audio_player._inflight_resolves.clear()

    tearDown = setUp


class TestClaimRelease(InflightTestCase):

    def test_first_claim_wins_second_gets_the_entry(self):
        self.assertIsNone(_claim_resolve(VID))
        waiter = _claim_resolve(VID)
        self.assertIsInstance(waiter.event, threading.Event)
        self.assertFalse(waiter.event.is_set())
        self.assertIsNone(waiter.result)

    def test_release_publishes_result_and_clears(self):
        _claim_resolve(VID)
        waiter = _claim_resolve(VID)
        payload = _info()
        _release_resolve(VID, payload)
        self.assertTrue(waiter.event.is_set())
        self.assertIs(waiter.result, payload, "the waiter must see the leader's result")
        self.assertNotIn(VID, audio_player._inflight_resolves)
        self.assertIsNone(_claim_resolve(VID), "slot must be reclaimable afterwards")

    def test_release_without_result_signals_failure(self):
        _claim_resolve(VID)
        waiter = _claim_resolve(VID)
        _release_resolve(VID)
        self.assertTrue(waiter.event.is_set())
        self.assertIsNone(waiter.result, "no result means the follower self-serves")

    def test_release_of_unknown_id_is_safe(self):
        _release_resolve("never-claimed")

    def test_different_videos_do_not_block_each_other(self):
        self.assertIsNone(_claim_resolve("vid-a"))
        self.assertIsNone(_claim_resolve("vid-b"))


class TestDeduplication(InflightTestCase):

    def test_concurrent_callers_make_one_api_call(self):
        """The whole point: two threads racing for one video = one /player call."""
        calls = []
        started = threading.Event()

        def slow_resolve(query, client, debug=False, cookies_file=None, *, force_cli=False):
            calls.append(query)
            started.set()
            time.sleep(0.3)          # hold the claim so the follower must wait
            return _info()

        with patch.object(audio_player, "_resolve_audio_url", slow_resolve):
            results = {}

            def leader():
                results["leader"] = get_audio_url(WATCH, "web_embedded")

            def follower():
                started.wait(2)      # ensure the leader claimed first
                results["follower"] = get_audio_url(WATCH, "web_embedded")

            t1, t2 = threading.Thread(target=leader), threading.Thread(target=follower)
            t1.start(); t2.start(); t1.join(10); t2.join(10)

        self.assertEqual(len(calls), 1, f"expected one resolve, got {len(calls)}")
        self.assertEqual(results["leader"]["title"], "Song")
        self.assertEqual(results["follower"]["title"], "Song",
                         "the follower must get the leader's result")

    def test_follower_gets_an_independent_copy(self):
        with patch.object(audio_player, "_resolve_audio_url",
                          lambda *a, **k: _info()):
            first = get_audio_url(WATCH, "web_embedded")
        first["title"] = "MUTATED"
        with patch.object(audio_player, "_resolve_audio_url",
                          lambda *a, **k: _info()):
            second = get_audio_url(WATCH, "web_embedded")
        self.assertEqual(second["title"], "Song")

    def test_claim_released_when_resolve_raises(self):
        """A failed resolve must not wedge the video id forever."""
        def boom(*a, **k):
            raise RuntimeError("resolve failed")

        with patch.object(audio_player, "_resolve_audio_url", boom):
            with self.assertRaises(RuntimeError):
                get_audio_url(WATCH, "web_embedded")
        self.assertNotIn(VID, audio_player._inflight_resolves)

    def test_follower_self_serves_when_leader_fails(self):
        """Waiting on a leader that produced nothing must not fail the follower."""
        _claim_resolve(VID)                    # simulate an in-flight leader
        _release_resolve(VID)                  # ...that finished without caching
        with patch.object(audio_player, "_resolve_audio_url",
                          lambda *a, **k: _info()) as _:
            result = get_audio_url(WATCH, "web_embedded")
        self.assertEqual(result["title"], "Song")

    def test_cache_hit_short_circuits_before_claiming(self):
        audio_player._resolve_cache_put(VID, _info())
        with patch.object(audio_player, "_resolve_audio_url") as m:
            get_audio_url(WATCH, "web_embedded")
        m.assert_not_called()
        self.assertNotIn(VID, audio_player._inflight_resolves)

    def test_text_query_bypasses_dedup(self):
        """No video id to key on — must not claim anything or wait."""
        with patch.object(audio_player, "_resolve_audio_url",
                          lambda *a, **k: _info()):
            get_audio_url("some search text", "web_embedded")
        self.assertEqual(audio_player._inflight_resolves, {})

    def test_force_cli_bypasses_cache_and_dedup(self):
        """The degraded-resolve retry must always do real work."""
        audio_player._resolve_cache_put(VID, _info(title="stale"))
        with patch.object(audio_player, "_resolve_audio_url",
                          lambda *a, **k: _info(title="fresh")) as _:
            result = get_audio_url(WATCH, "web_embedded", force_cli=True)
        self.assertEqual(result["title"], "fresh")
        self.assertEqual(audio_player._inflight_resolves, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
