"""Tests for the PO-token provider config and the video-id resolve cache.

Both are play-latency fixes: the bgutil HTTP server replaces a dead-port probe that
cost ~2s per resolve, and repeat plays of the same video skip the resolve entirely.
No network — the cache helpers are exercised directly.
"""
import os
import sys
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

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
        # googlevideo answers 403 to URLs carrying the bundled bgutil build's token, so no
        # provider may be asked ("never"); "missing_pot" keeps the token-less formats that
        # yt-dlp would otherwise drop once web_embedded has a token policy.
        self.assertEqual(args["youtube"]["fetch_pot"], ["never"])
        self.assertEqual(args["youtube"]["formats"], ["missing_pot"])

    def test_subprocess_command_line_carries_same_values(self):
        with patch.object(audio_player.subprocess, "Popen") as popen:
            audio_player._start_ytdlp_stream(WATCH.format("abc12345678"), "web_embedded",
                                             direct_url=None)
        cmd = popen.call_args.args[0]
        self.assertIn("youtube:player_client=web_embedded;fetch_pot=never;formats=missing_pot", cmd)


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
        first["url"] = "https://evil/"
        second = _resolve_cache_get(WATCH.format("abc12345678"))
        self.assertIsNot(first, second)
        self.assertEqual(second["title"], "Track abc12345678")
        self.assertNotEqual(second["url"], "https://evil/")

    def test_put_snapshots_the_caller_dict(self):
        """Mutating the dict after caching must not rewrite the cached entry."""
        info = _info("abc12345678")
        _resolve_cache_put("abc12345678", info)
        info["title"] = "MUTATED AFTER PUT"
        self.assertEqual(_resolve_cache_get(WATCH.format("abc12345678"))["title"],
                         "Track abc12345678")

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


class TestStreamClient(unittest.TestCase):

    def test_reads_the_c_param(self):
        self.assertEqual(audio_player._stream_client(
            "https://r1.googlevideo.com/videoplayback?expire=1&c=WEB_EMBEDDED_PLAYER&clen=5"),
            "WEB_EMBEDDED_PLAYER")
        self.assertEqual(audio_player._stream_client(
            "https://r1.googlevideo.com/videoplayback?c=VISIONOS&expire=1"), "VISIONOS")

    def test_absent_param_is_empty(self):
        self.assertEqual(audio_player._stream_client(
            "https://r1.googlevideo.com/videoplayback?expire=1&id=abc"), "")
        self.assertEqual(audio_player._stream_client("https://r1.googlevideo.com/videoplayback"), "")
        self.assertEqual(audio_player._stream_client(""), "")

    def test_unparsable_value_is_empty(self):
        self.assertEqual(audio_player._stream_client("http://[bad/videoplayback?c=X"), "")
        self.assertEqual(audio_player._stream_client(None), "")


class TestResolveRecordsClientAndTime(unittest.TestCase):
    """The result of a lookup says which client the URL came from and when it was minted."""

    def setUp(self):
        audio_player._resolve_cache.clear()

    tearDown = setUp

    def _resolve(self, url, query=WATCH.format("abc12345678")):
        info = {"id": "abc12345678", "url": url, "vcodec": "none", "title": "T",
                "thumbnail": "", "webpage_url": query, "duration": 100}

        @contextmanager
        def fake_resolver(key, opts):
            yield SimpleNamespace(extract_info=lambda q, download=False: dict(info))

        with patch.object(audio_player, "_resolver", fake_resolver):
            return audio_player._resolve_audio_url(query, "web_embedded")

    def test_youtube_lookup_carries_client_and_mint_time(self):
        before = time.time()
        out = self._resolve("https://r1.googlevideo.com/videoplayback?expire=99999999999&c=VISIONOS")
        self.assertEqual(out["client"], "VISIONOS")
        self.assertLessEqual(before, out["minted_at"])
        self.assertAlmostEqual(out["minted_at"], time.time(), delta=1)

    def test_url_without_c_gives_empty_client(self):
        out = self._resolve("https://r1.googlevideo.com/videoplayback?expire=99999999999")
        self.assertEqual(out["client"], "")
        self.assertIn("minted_at", out)

    def test_non_youtube_url_gives_empty_client(self):
        out = self._resolve("https://cf-media.sndcdn.com/track.mp3?Policy=x",
                            query="https://soundcloud.com/artist/track")
        self.assertEqual(out["client"], "")

    def test_cache_hit_returns_both_keys_unchanged(self):
        out = self._resolve("https://r1.googlevideo.com/videoplayback?expire=99999999999&c=VISIONOS")
        hit = _resolve_cache_get(WATCH.format("abc12345678"))
        self.assertIsNotNone(hit)
        self.assertEqual(hit["client"], "VISIONOS")
        self.assertEqual(hit["minted_at"], out["minted_at"])


class TestPlayInvalidatesCache(unittest.TestCase):
    """A dead cached URL must not be handed straight back to play()'s retry."""

    def test_play_calls_invalidate_on_cached_failure(self):
        import inspect
        src = inspect.getsource(audio_player.AudioPlayer._start_playback)   # play()'s body
        self.assertEqual(src.count("invalidate_resolve_cache(url_or_query)"), 2,
                         "both cached-URL failure branches must invalidate the cache")


if __name__ == "__main__":
    unittest.main(verbosity=2)
