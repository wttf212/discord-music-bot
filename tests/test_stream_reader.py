"""Tests for the in-process curl_cffi CDN reader that replaced the yt-dlp stream
subprocess for googlevideo progressive audio.

No network: a fake curl_cffi Session is injected. The behaviours pinned here are the
ones probed live against googlevideo (see the plan) — range windows, the settle-window
403 retry, no Cookie header, sequential-only requests, EOF from `clen=`.
"""
import os
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_player
from audio_player import (
    _can_stream_in_process,
    _range_url,
    _stream_total_bytes,
    _stream_error_text,
    _CurlStreamReader,
    _open_audio_stream,
    _STREAM_CHUNK_BYTES,
)

GV = "https://r5---sn-x.googlevideo.com/videoplayback?expire=99999999999&id=abc&clen={}"


class FakeResponse:
    def __init__(self, status_code, body=b"", content_length=None):
        self.status_code = status_code
        self._body = body
        self.headers = {}
        if content_length is not None:
            self.headers["content-length"] = str(content_length)
        elif status_code in (200, 206):
            self.headers["content-length"] = str(len(body))
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    """Records every request so we can assert on ranges, headers and ordering."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []          # (url, kwargs)
        self.concurrent = 0
        self.max_concurrent = 0
        self.closed = False
        self._lock = threading.Lock()

    def get(self, url, **kwargs):
        with self._lock:
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            self.requests.append((url, kwargs))
            if not self._responses:
                return FakeResponse(404)
            resp = self._responses.pop(0)
            if isinstance(resp, Exception):
                raise resp
            return resp
        finally:
            with self._lock:
                self.concurrent -= 1

    def close(self):
        self.closed = True


def _reader(responses, url=None, debug=False):
    """Build a reader against a FakeSession; returns (reader_or_exc, session)."""
    session = FakeSession(responses)

    class FakeRequests:
        @staticmethod
        def Session(**kwargs):
            session.impersonate = kwargs.get("impersonate")
            return session

    fake_module = type(sys)("curl_cffi.requests")
    fake_module.Session = FakeRequests.Session
    with patch.dict(sys.modules, {"curl_cffi.requests": fake_module,
                                  "curl_cffi": type(sys)("curl_cffi")}):
        sys.modules["curl_cffi"].requests = fake_module
        try:
            return _CurlStreamReader(url or GV.format(1024), debug=debug), session
        except Exception as e:
            return e, session


def _drain(reader):
    data = reader.stdout.read()
    reader.wait(timeout=5)
    return data


class TestHelpers(unittest.TestCase):

    def test_range_url_appends_to_existing_query(self):
        self.assertEqual(_range_url("https://h/v?a=1", 0, 9), "https://h/v?a=1&range=0-9")

    def test_range_url_starts_query_when_absent(self):
        self.assertEqual(_range_url("https://h/v", 5, 9), "https://h/v?range=5-9")

    def test_total_bytes_from_clen(self):
        self.assertEqual(_stream_total_bytes(GV.format(3433755)), 3433755)

    def test_total_bytes_absent(self):
        self.assertIsNone(_stream_total_bytes("https://h/v?a=1"))

    def test_error_text_prefers_stderr(self):
        class P:
            stderr = type("S", (), {"read": staticmethod(lambda: b"boom")})()
        self.assertEqual(_stream_error_text(P()), "boom")

    def test_error_text_falls_back_to_attribute(self):
        class R:
            stderr = None
            error_text = "HTTP 403 at offset 0"
        self.assertEqual(_stream_error_text(R()), "HTTP 403 at offset 0")


class TestCapabilityGate(unittest.TestCase):
    """Deliberately narrow — only googlevideo progressive audio."""

    def _info(self, **over):
        base = {"url": GV.format(1024), "protocol": "https", "is_live": False}
        base.update(over)
        return base

    def test_accepts_googlevideo_progressive(self):
        with patch.object(audio_player, "_IMPERSONATE_AVAILABLE", True):
            self.assertTrue(_can_stream_in_process(self._info()))

    def test_rejects_when_impersonation_unavailable(self):
        with patch.object(audio_player, "_IMPERSONATE_AVAILABLE", False):
            self.assertFalse(_can_stream_in_process(self._info()))

    def test_rejects_live(self):
        with patch.object(audio_player, "_IMPERSONATE_AVAILABLE", True):
            self.assertFalse(_can_stream_in_process(self._info(is_live=True)))

    def test_rejects_hls(self):
        with patch.object(audio_player, "_IMPERSONATE_AVAILABLE", True):
            self.assertFalse(_can_stream_in_process(self._info(protocol="m3u8_native")))

    def test_rejects_non_googlevideo_host(self):
        with patch.object(audio_player, "_IMPERSONATE_AVAILABLE", True):
            self.assertFalse(_can_stream_in_process(
                self._info(url="https://cf-media.sndcdn.com/x.mp3")))

    def test_rejects_lookalike_host(self):
        with patch.object(audio_player, "_IMPERSONATE_AVAILABLE", True):
            self.assertFalse(_can_stream_in_process(
                self._info(url="https://evilgooglevideo.com/videoplayback?clen=1")))

    def test_rejects_missing_info(self):
        with patch.object(audio_player, "_IMPERSONATE_AVAILABLE", True):
            self.assertFalse(_can_stream_in_process(None))
            self.assertFalse(_can_stream_in_process({}))


class TestReaderHappyPath(unittest.TestCase):

    def test_single_window_streams_all_bytes(self):
        body = b"\xff" * 4096
        reader, session = _reader([FakeResponse(200, body)], url=GV.format(len(body)))
        self.assertEqual(_drain(reader), body)
        self.assertEqual(reader.poll(), 0)
        self.assertEqual(len(session.requests), 1)

    def test_request_uses_range_param_and_clamps_to_clen(self):
        body = b"x" * 100
        reader, session = _reader([FakeResponse(200, body)], url=GV.format(100))
        _drain(reader)
        self.assertIn("&range=0-99", session.requests[0][0],
                      "window must be clamped to clen-1, not chunk size")

    def test_never_sends_cookies_or_ua(self):
        reader, session = _reader([FakeResponse(200, b"a" * 16)], url=GV.format(16))
        _drain(reader)
        _, kwargs = session.requests[0]
        headers = kwargs.get("headers") or {}
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("User-Agent", headers)

    def test_uses_chrome_impersonation(self):
        reader, session = _reader([FakeResponse(200, b"a" * 16)], url=GV.format(16))
        _drain(reader)
        self.assertEqual(session.impersonate, "chrome")

    def test_multiple_windows_are_sequential(self):
        first = b"a" * _STREAM_CHUNK_BYTES
        second = b"b" * 32
        total = len(first) + len(second)
        reader, session = _reader([FakeResponse(200, first), FakeResponse(200, second)],
                                  url=GV.format(total))
        self.assertEqual(len(_drain(reader)), total)
        self.assertEqual(len(session.requests), 2)
        self.assertIn(f"&range={_STREAM_CHUNK_BYTES}-{total - 1}", session.requests[1][0])
        self.assertEqual(session.max_concurrent, 1,
                         "range windows must never be fetched in parallel")

    def test_window_count_matches_old_chunk_size(self):
        """10 MiB windows == the old --http-chunk-size 10M request count."""
        self.assertEqual(_STREAM_CHUNK_BYTES, 10 * 1024 * 1024)

    def test_eof_without_clen_on_short_window(self):
        reader, session = _reader([FakeResponse(200, b"z" * 64)],
                                  url="https://r1.googlevideo.com/videoplayback?id=x")
        self.assertEqual(_drain(reader), b"z" * 64)
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(reader.poll(), 0,
                         "a clean EOF without clen= is success, not failure")


class TestReaderFailureHandling(unittest.TestCase):

    def test_first_window_403_retries_then_succeeds(self):
        """The CDN settle window: a fresh URL 403s for ~2-3s before it serves."""
        body = b"ok" * 8
        with patch.object(audio_player, "_STREAM_SETTLE_BACKOFF", (0.01, 0.01, 0.01)):
            reader, session = _reader(
                [FakeResponse(403), FakeResponse(403), FakeResponse(200, body)],
                url=GV.format(len(body)))
        self.assertEqual(_drain(reader), body)
        self.assertEqual(len(session.requests), 3)

    def test_first_window_exhausted_raises_for_fallback(self):
        with patch.object(audio_player, "_STREAM_SETTLE_BACKOFF", (0.01,)):
            result, session = _reader([FakeResponse(403), FakeResponse(403)])
        self.assertIsInstance(result, Exception)
        self.assertIn("403", str(result))
        self.assertTrue(session.closed, "session must be closed when start fails")

    def test_connection_error_on_first_window_is_retried(self):
        body = b"q" * 16
        with patch.object(audio_player, "_STREAM_SETTLE_BACKOFF", (0.01, 0.01)):
            reader, session = _reader([ConnectionError("reset"), FakeResponse(200, body)],
                                      url=GV.format(len(body)))
        self.assertEqual(_drain(reader), body)

    def test_midstream_403_stops_instead_of_hammering(self):
        """Mid-stream 403 means the URL expired — give up, do not retry-loop."""
        first = b"a" * _STREAM_CHUNK_BYTES
        reader, session = _reader([FakeResponse(200, first), FakeResponse(403)],
                                  url=GV.format(_STREAM_CHUNK_BYTES + 4096))
        self.assertEqual(len(_drain(reader)), _STREAM_CHUNK_BYTES)
        self.assertEqual(len(session.requests), 2, "no retry storm on an expired URL")
        self.assertEqual(reader.poll(), 1)

    def test_truncated_window_resumes_from_offset(self):
        """A short read against a longer Content-Length is a blip, not EOF."""
        total = _STREAM_CHUNK_BYTES + 100
        truncated = FakeResponse(200, b"a" * 4096, content_length=_STREAM_CHUNK_BYTES)
        rest = FakeResponse(200, b"b" * (total - 4096))
        reader, session = _reader([truncated, rest], url=GV.format(total))
        self.assertEqual(len(_drain(reader)), total)
        self.assertIn("&range=4096-", session.requests[1][0])

    def test_zero_byte_window_does_not_spin(self):
        reader, session = _reader([FakeResponse(200, b"", content_length=1024)],
                                  url=GV.format(4096))
        _drain(reader)
        self.assertEqual(len(session.requests), 1)


class TestPopenCompatibility(unittest.TestCase):
    """play() and stop_playback() drive this like a subprocess.Popen handle."""

    def test_poll_none_while_running_then_zero(self):
        body = b"a" * 64
        reader, _ = _reader([FakeResponse(200, body)], url=GV.format(len(body)))
        _drain(reader)
        self.assertEqual(reader.poll(), 0)

    def test_terminate_is_idempotent_and_unblocks(self):
        body = b"a" * 64
        reader, _ = _reader([FakeResponse(200, body)], url=GV.format(len(body)))
        reader.terminate()
        reader.terminate()
        reader.kill()

    def test_wait_timeout_raises_timeoutexpired(self):
        """_reap_process() relies on this exact exception type.

        Built without starting a pump thread so the still-running state is
        deterministic — this pins the wait() contract, not the pump's timing.
        """
        reader = _CurlStreamReader.__new__(_CurlStreamReader)
        reader._finished = threading.Event()
        reader._returncode = None
        with self.assertRaises(subprocess.TimeoutExpired):
            reader.wait(timeout=0.01)
        reader._returncode = 0
        reader._finished.set()
        self.assertEqual(reader.wait(timeout=0.01), 0)

    def test_has_popen_surface(self):
        body = b"a" * 64
        reader, _ = _reader([FakeResponse(200, body)], url=GV.format(len(body)))
        for attr in ("stdout", "stderr", "pid", "poll", "terminate", "kill", "wait"):
            self.assertTrue(hasattr(reader, attr), f"missing Popen attribute {attr}")
        self.assertIsNone(reader.stderr, "no child process means no stderr to drain")
        _drain(reader)


class TestStreamFactory(unittest.TestCase):

    def test_non_googlevideo_uses_subprocess(self):
        info = {"url": "https://cf-media.sndcdn.com/x.mp3", "protocol": "https"}
        with patch.object(audio_player, "_start_ytdlp_stream") as sub:
            _open_audio_stream("q", "web_embedded", None, info)
        sub.assert_called_once_with("q", "web_embedded", None, info["url"])

    def test_reader_failure_falls_back_to_subprocess(self):
        """Reliability can only improve: if the reader cannot open, yt-dlp still tries."""
        info = {"url": GV.format(1024), "protocol": "https", "is_live": False}
        with patch.object(audio_player, "_IMPERSONATE_AVAILABLE", True), \
             patch.object(audio_player, "_CurlStreamReader",
                          side_effect=audio_player._StreamStartError("HTTP 403")), \
             patch.object(audio_player, "_start_ytdlp_stream") as sub:
            _open_audio_stream("q", "web_embedded", None, info)
        sub.assert_called_once()

    def test_googlevideo_uses_reader(self):
        info = {"url": GV.format(1024), "protocol": "https", "is_live": False}
        with patch.object(audio_player, "_IMPERSONATE_AVAILABLE", True), \
             patch.object(audio_player, "_CurlStreamReader") as reader, \
             patch.object(audio_player, "_start_ytdlp_stream") as sub:
            _open_audio_stream("q", "web_embedded", None, info)
        reader.assert_called_once()
        sub.assert_not_called()

    def test_cookies_never_reach_the_cdn_reader(self):
        """cookies_file is accepted for the subprocess path but must not be forwarded."""
        info = {"url": GV.format(1024), "protocol": "https", "is_live": False}
        with patch.object(audio_player, "_IMPERSONATE_AVAILABLE", True), \
             patch.object(audio_player, "_CurlStreamReader") as reader:
            _open_audio_stream("q", "web_embedded", "/tmp/cookies.txt", info)
        _, kwargs = reader.call_args
        self.assertNotIn("cookies_file", kwargs)
        self.assertNotIn("/tmp/cookies.txt", str(reader.call_args))


if __name__ == "__main__":
    unittest.main(verbosity=2)
