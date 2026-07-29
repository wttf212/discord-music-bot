"""Tests for the PO-token provider config and the video-id resolve cache.

Both are play-latency fixes: the bgutil HTTP server replaces a dead-port probe that
cost ~2s per resolve, and repeat plays of the same video skip the resolve entirely.
No network — the cache helpers are exercised directly.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_player
from audio_player import (
    _youtube_extractor_args,
    _resolve_cache_get,
    _resolve_cache_put,
    invalidate_resolve_cache,
    _RESOLVE_CACHE_MAX,
    STREAM_URL_SAFETY_MARGIN,
)

WATCH = "https://www.youtube.com/watch?v={}"


def _info(video_id="abc12345678", expire_in=6 * 3600, audio_only=True):
    return {
        "url": f"https://r5---sn-x.googlevideo.com/videoplayback?expire={int(time.time()) + expire_in}&id={video_id}",
        "title": f"Track {video_id}",
        "http_headers": {"User-Agent": "UA"},
        "thumbnail": "",
        "webpage_url": WATCH.format(video_id),
        "is_audio_only": audio_only,
        "duration": 120,
        "artist": "",
    }


class TestYoutubeExtractorArgs(unittest.TestCase):
    """The dead-port redirect must appear ONLY on the CLI-forced retry path."""

    def test_default_leaves_http_provider_alone(self):
        args = _youtube_extractor_args("web_embedded", "/opt/bgutil-pot")
        self.assertNotIn("youtubepot-bgutilhttp", args,
                         "default path must not probe a dead port (~2s per resolve)")

    def test_default_registers_cli_as_fallback(self):
        args = _youtube_extractor_args("web_embedded", "/opt/bgutil-pot")
        self.assertEqual(args["youtubepot-bgutilcli"], {"cli_path": ["/opt/bgutil-pot"]})

    def test_force_cli_redirects_http_to_dead_port(self):
        args = _youtube_extractor_args("web_embedded", "/opt/bgutil-pot", force_cli=True)
        self.assertEqual(args["youtubepot-bgutilhttp"],
                         {"base_url": ["http://127.0.0.1:1"]})
        self.assertIn("youtubepot-bgutilcli", args)

    def test_no_bgutil_binary_means_no_provider_args(self):
        args = _youtube_extractor_args("web_embedded", None, force_cli=True)
        self.assertNotIn("youtubepot-bgutilcli", args)
        self.assertNotIn("youtubepot-bgutilhttp", args)

    def test_anti_detection_args_preserved(self):
        args = _youtube_extractor_args("web,android_vr", None)
        self.assertEqual(args["youtube"]["player_client"], ["web", "android_vr"])
        self.assertEqual(args["youtube"]["fetch_pot"], ["always"])


class TestResolveCache(unittest.TestCase):

    def setUp(self):
        audio_player._resolve_cache.clear()

    tearDown = setUp

    def test_put_then_get_by_watch_url(self):
        _resolve_cache_put("abc12345678", _info("abc12345678"))
        hit = _resolve_cache_get(WATCH.format("abc12345678"))
        self.assertIsNotNone(hit)
        self.assertEqual(hit["title"], "Track abc12345678")

    def test_get_by_short_url(self):
        _resolve_cache_put("abc12345678", _info("abc12345678"))
        self.assertIsNotNone(_resolve_cache_get("https://youtu.be/abc12345678"))

    def test_miss_for_unknown_video(self):
        self.assertIsNone(_resolve_cache_get(WATCH.format("zzz99999999")))

    def test_text_query_never_hits(self):
        _resolve_cache_put("abc12345678", _info("abc12345678"))
        self.assertIsNone(_resolve_cache_get("rick astley never gonna give you up"))

    def test_non_youtube_url_never_hits(self):
        _resolve_cache_put("abc12345678", _info("abc12345678"))
        self.assertIsNone(_resolve_cache_get("https://soundcloud.com/artist/track"))

    def test_expired_entry_is_dropped(self):
        stale = _info("abc12345678", expire_in=STREAM_URL_SAFETY_MARGIN - 60)
        _resolve_cache_put("abc12345678", stale)
        self.assertIsNone(_resolve_cache_get(WATCH.format("abc12345678")))
        self.assertNotIn("abc12345678", audio_player._resolve_cache,
                         "stale entry should be evicted on read")

    def test_degraded_format_not_cached(self):
        """A combined video+audio resolve means attestation broke — never pin it."""
        _resolve_cache_put("abc12345678", _info("abc12345678", audio_only=False))
        self.assertIsNone(_resolve_cache_get(WATCH.format("abc12345678")))

    def test_urlless_result_not_cached(self):
        broken = _info("abc12345678")
        broken["url"] = ""
        _resolve_cache_put("abc12345678", broken)
        self.assertEqual(len(audio_player._resolve_cache), 0)

    def test_missing_video_id_not_cached(self):
        _resolve_cache_put(None, _info("abc12345678"))
        self.assertEqual(len(audio_player._resolve_cache), 0)

    def test_invalidate_removes_entry(self):
        _resolve_cache_put("abc12345678", _info("abc12345678"))
        invalidate_resolve_cache(WATCH.format("abc12345678"))
        self.assertIsNone(_resolve_cache_get(WATCH.format("abc12345678")))

    def test_invalidate_tolerates_text_query(self):
        invalidate_resolve_cache("some search text")  # must not raise

    def test_hit_is_a_copy(self):
        _resolve_cache_put("abc12345678", _info("abc12345678"))
        first = _resolve_cache_get(WATCH.format("abc12345678"))
        first["title"] = "MUTATED"
        first["http_headers"]["Cookie"] = "leaked"
        second = _resolve_cache_get(WATCH.format("abc12345678"))
        self.assertEqual(second["title"], "Track abc12345678")
        self.assertNotIn("Cookie", second["http_headers"])

    def test_lru_bound_evicts_oldest(self):
        for i in range(_RESOLVE_CACHE_MAX + 20):
            vid = f"vid{i:08d}"
            _resolve_cache_put(vid, _info(vid))
        self.assertEqual(len(audio_player._resolve_cache), _RESOLVE_CACHE_MAX)
        self.assertIsNone(_resolve_cache_get(WATCH.format("vid00000000")))
        self.assertIsNotNone(_resolve_cache_get(WATCH.format(f"vid{_RESOLVE_CACHE_MAX + 19:08d}")))

    def test_read_refreshes_lru_position(self):
        for i in range(_RESOLVE_CACHE_MAX):
            vid = f"vid{i:08d}"
            _resolve_cache_put(vid, _info(vid))
        _resolve_cache_get(WATCH.format("vid00000000"))  # touch the oldest
        _resolve_cache_put("newvideo001", _info("newvideo001"))
        self.assertIsNotNone(_resolve_cache_get(WATCH.format("vid00000000")))
        self.assertIsNone(_resolve_cache_get(WATCH.format("vid00000001")))


class TestPlayInvalidatesCache(unittest.TestCase):
    """A dead cached URL must not be handed straight back to play()'s retry."""

    def test_play_calls_invalidate_on_cached_failure(self):
        import inspect
        src = inspect.getsource(audio_player.AudioPlayer.play)
        self.assertEqual(src.count("invalidate_resolve_cache(url_or_query)"), 2,
                         "both cached-URL failure branches must invalidate the cache")


if __name__ == "__main__":
    unittest.main(verbosity=2)
