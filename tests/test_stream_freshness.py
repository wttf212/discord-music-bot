"""Tests for stream-URL freshness helpers used by the prefetch/cache reuse path.

is_stream_info_fresh decides whether a cached get_audio_url() result can be
streamed again without re-resolving. Primary guard is the CDN URL's own
`expire=` timestamp; fallback is a wall-clock TTL for expiry-less URLs.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audio_player import (
    is_stream_info_fresh,
    _stream_url_expiry,
    STREAM_URL_SAFETY_MARGIN,
    STREAM_INFO_FALLBACK_TTL,
)


def _gv(expire_epoch: int) -> str:
    return f"https://r5---sn-x.googlevideo.com/videoplayback?expire={expire_epoch}&id=abc"


class TestStreamUrlExpiry(unittest.TestCase):
    def test_parses_expire(self):
        self.assertEqual(_stream_url_expiry(_gv(1234567890)), 1234567890.0)

    def test_no_expire_param(self):
        self.assertIsNone(_stream_url_expiry("https://soundcloud.com/x/y"))

    def test_empty_url(self):
        self.assertIsNone(_stream_url_expiry(""))

    def test_malformed_expire(self):
        self.assertIsNone(_stream_url_expiry("https://x/y?expire=notanumber"))


class TestIsStreamInfoFresh(unittest.TestCase):
    def test_none_info(self):
        self.assertFalse(is_stream_info_fresh(None))

    def test_missing_url(self):
        self.assertFalse(is_stream_info_fresh({"title": "x"}))

    def test_empty_url(self):
        self.assertFalse(is_stream_info_fresh({"url": ""}))

    def test_expire_far_future_is_fresh(self):
        now = time.time()
        info = {"url": _gv(int(now) + 6 * 3600)}
        self.assertTrue(is_stream_info_fresh(info, now=now))

    def test_expire_within_margin_is_stale(self):
        now = time.time()
        info = {"url": _gv(int(now) + STREAM_URL_SAFETY_MARGIN - 60)}
        self.assertFalse(is_stream_info_fresh(info, now=now))

    def test_expire_past_is_stale(self):
        now = time.time()
        info = {"url": _gv(int(now) - 10)}
        self.assertFalse(is_stream_info_fresh(info, now=now))

    def test_no_expire_recent_resolve_is_fresh(self):
        now = time.time()
        info = {"url": "https://soundcloud.com/stream/x"}
        self.assertTrue(is_stream_info_fresh(info, resolved_at=now - 60, now=now))

    def test_no_expire_old_resolve_is_stale(self):
        now = time.time()
        info = {"url": "https://soundcloud.com/stream/x"}
        self.assertFalse(is_stream_info_fresh(info, resolved_at=now - STREAM_INFO_FALLBACK_TTL - 60, now=now))

    def test_no_expire_no_timestamp_is_stale(self):
        info = {"url": "https://soundcloud.com/stream/x"}
        self.assertFalse(is_stream_info_fresh(info, resolved_at=0.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
