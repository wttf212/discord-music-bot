"""Tests for retry-with-backoff helpers (RETRY-01 — Phase 04-01).

These tests exercise _is_retryable_ytdlp_error, _retry_with_backoff, and
get_audio_url_with_retry WITHOUT hitting the network. All sleep calls are
patched so the tests run instantly.
"""
import random
import sys
import os
import unittest
from unittest.mock import patch

# Ensure the project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from yt_dlp.utils import DownloadError
import audio_player
from audio_player import (
    _is_retryable_ytdlp_error,
    _retry_with_backoff,
    get_audio_url_with_retry,
)


# ---------------------------------------------------------------------------
# _is_retryable_ytdlp_error classifier
# ---------------------------------------------------------------------------

class TestClassifier(unittest.TestCase):

    # --- retryable by message ---

    def test_http_429_retryable(self):
        exc = DownloadError("ERROR: [youtube] abc: HTTP Error 429: Too Many Requests")
        self.assertTrue(_is_retryable_ytdlp_error(exc))

    def test_http_503_retryable(self):
        exc = DownloadError("ERROR: [youtube] abc: HTTP Error 503: Service Unavailable")
        self.assertTrue(_is_retryable_ytdlp_error(exc))

    def test_http_504_retryable(self):
        exc = DownloadError("ERROR: [youtube] abc: HTTP Error 504: Gateway Timeout")
        self.assertTrue(_is_retryable_ytdlp_error(exc))

    def test_too_many_requests_retryable(self):
        exc = DownloadError("too many requests")
        self.assertTrue(_is_retryable_ytdlp_error(exc))

    def test_connection_reset_retryable(self):
        exc = DownloadError("Connection reset by peer")
        self.assertTrue(_is_retryable_ytdlp_error(exc))

    def test_timed_out_retryable(self):
        exc = DownloadError("timed out")
        self.assertTrue(_is_retryable_ytdlp_error(exc))

    def test_read_timed_out_retryable(self):
        exc = DownloadError("Read timed out")
        self.assertTrue(_is_retryable_ytdlp_error(exc))

    def test_temporary_failure_dns_retryable(self):
        exc = DownloadError("Temporary failure in name resolution")
        self.assertTrue(_is_retryable_ytdlp_error(exc))

    # --- retryable by type ---

    def test_connection_error_retryable_by_type(self):
        self.assertTrue(_is_retryable_ytdlp_error(ConnectionError("Connection reset by peer")))

    def test_timeout_error_retryable_by_type(self):
        self.assertTrue(_is_retryable_ytdlp_error(TimeoutError("read timeout")))

    def test_socket_timeout_retryable_by_type(self):
        import socket
        self.assertTrue(_is_retryable_ytdlp_error(socket.timeout("timed out")))

    def test_url_error_retryable_by_type(self):
        from urllib.error import URLError
        self.assertTrue(_is_retryable_ytdlp_error(URLError("connection refused")))

    # --- non-retryable by message ---

    def test_video_unavailable_non_retryable(self):
        exc = DownloadError("ERROR: [youtube] abc: Video unavailable")
        self.assertFalse(_is_retryable_ytdlp_error(exc))

    def test_sign_in_to_confirm_non_retryable(self):
        exc = DownloadError("ERROR: [youtube] abc: Sign in to confirm your age")
        self.assertFalse(_is_retryable_ytdlp_error(exc))

    def test_age_restricted_hyphen_non_retryable(self):
        exc = DownloadError("ERROR: [youtube] abc: This video is age-restricted")
        self.assertFalse(_is_retryable_ytdlp_error(exc))

    def test_age_restricted_space_non_retryable(self):
        exc = DownloadError("ERROR: [youtube] abc: age restricted")
        self.assertFalse(_is_retryable_ytdlp_error(exc))

    def test_not_available_in_your_country_non_retryable(self):
        exc = DownloadError("ERROR: [youtube] abc: This video is not available in your country")
        self.assertFalse(_is_retryable_ytdlp_error(exc))

    def test_private_video_non_retryable(self):
        exc = DownloadError("ERROR: [youtube] abc: Private video")
        self.assertFalse(_is_retryable_ytdlp_error(exc))

    def test_video_removed_non_retryable(self):
        exc = DownloadError("ERROR: [youtube] abc: This video has been removed")
        self.assertFalse(_is_retryable_ytdlp_error(exc))

    def test_copyright_non_retryable(self):
        exc = DownloadError("ERROR: copyright claim")
        self.assertFalse(_is_retryable_ytdlp_error(exc))

    def test_members_only_hyphen_non_retryable(self):
        exc = DownloadError("ERROR: members-only content")
        self.assertFalse(_is_retryable_ytdlp_error(exc))

    def test_members_only_space_non_retryable(self):
        exc = DownloadError("ERROR: members only")
        self.assertFalse(_is_retryable_ytdlp_error(exc))

    def test_requires_payment_non_retryable(self):
        exc = DownloadError("ERROR: requires payment")
        self.assertFalse(_is_retryable_ytdlp_error(exc))

    def test_this_live_event_non_retryable(self):
        exc = DownloadError("ERROR: this live event has ended")
        self.assertFalse(_is_retryable_ytdlp_error(exc))

    def test_confirm_your_age_non_retryable(self):
        exc = DownloadError("ERROR: confirm your age to watch this video")
        self.assertFalse(_is_retryable_ytdlp_error(exc))

    # --- ambiguous → retryable ---

    def test_ambiguous_download_error_retryable(self):
        exc = DownloadError("some unfamiliar error text")
        self.assertTrue(_is_retryable_ytdlp_error(exc))

    def test_ambiguous_value_error_retryable(self):
        self.assertTrue(_is_retryable_ytdlp_error(ValueError("totally unrelated")))

    def test_case_insensitive_non_retryable(self):
        exc = DownloadError("VIDEO UNAVAILABLE")
        self.assertFalse(_is_retryable_ytdlp_error(exc))

    def test_case_insensitive_retryable(self):
        exc = DownloadError("HTTP ERROR 429")
        self.assertTrue(_is_retryable_ytdlp_error(exc))


# ---------------------------------------------------------------------------
# _retry_with_backoff helper
# ---------------------------------------------------------------------------

class TestRetryWithBackoff(unittest.TestCase):

    def test_success_on_first_call(self):
        calls = []
        sleeps = []

        def fn(*a, **k):
            calls.append("ok")
            return {"title": "t", "url": "u"}

        with patch.object(audio_player.time, "sleep", side_effect=sleeps.append):
            result = _retry_with_backoff(fn, "q", "c", False, None, max_attempts=3, base_delay=5.0, jitter=0.25)

        self.assertEqual(result, {"title": "t", "url": "u"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(sleeps), 0)

    def test_retryable_succeeds_on_second_attempt(self):
        calls = []
        sleeps = []
        attempt_num = [0]

        def fn(*a, **k):
            attempt_num[0] += 1
            if attempt_num[0] == 1:
                calls.append("f")
                raise DownloadError("ERROR: HTTP Error 429: Too Many Requests")
            calls.append("ok")
            return {"title": "t", "url": "u"}

        with patch.object(audio_player.time, "sleep", side_effect=sleeps.append):
            result = _retry_with_backoff(fn, "q", "c", False, None, max_attempts=3, base_delay=5.0, jitter=0.25)

        self.assertEqual(result, {"title": "t", "url": "u"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], 5.0 * 0.75)  # 3.75
        self.assertLessEqual(sleeps[0], 5.0 * 1.25)     # 6.25

    def test_all_attempts_exhausted_raises_last_exception(self):
        calls = []
        sleeps = []

        def fn(*a, **k):
            calls.append("f")
            raise DownloadError("ERROR: HTTP Error 429: Too Many Requests")

        with patch.object(audio_player.time, "sleep", side_effect=sleeps.append):
            with self.assertRaises(DownloadError):
                _retry_with_backoff(fn, "q", "c", False, None, max_attempts=3, base_delay=5.0, jitter=0.25)

        self.assertEqual(len(calls), 3)
        self.assertEqual(len(sleeps), 2)
        # First sleep: base_delay * 2^0 = 5.0, jitter ±25% → [3.75, 6.25]
        self.assertGreaterEqual(sleeps[0], 3.75)
        self.assertLessEqual(sleeps[0], 6.25)
        # Second sleep: base_delay * 2^1 = 10.0, jitter ±25% → [7.5, 12.5]
        self.assertGreaterEqual(sleeps[1], 7.5)
        self.assertLessEqual(sleeps[1], 12.5)

    def test_non_retryable_raises_immediately_no_sleep(self):
        calls = []
        sleeps = []

        def fn(*a, **k):
            calls.append("p")
            raise DownloadError("ERROR: Video unavailable")

        with patch.object(audio_player.time, "sleep", side_effect=sleeps.append):
            with self.assertRaises(DownloadError):
                _retry_with_backoff(fn, "q", "c", False, None, max_attempts=3, base_delay=5.0, jitter=0.25)

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(sleeps), 0)

    def test_two_sleep_durations_are_different_tiers(self):
        """Verify the two sleep durations come from different exponent tiers (5s vs 10s base)."""
        sleeps = []

        def fn(*a, **k):
            raise DownloadError("ERROR: HTTP Error 429")

        with patch.object(audio_player.time, "sleep", side_effect=sleeps.append):
            with self.assertRaises(DownloadError):
                _retry_with_backoff(fn, "q", "c", False, None, max_attempts=3, base_delay=5.0, jitter=0.25)

        # Even at the extreme of jitter, tier 0 max (6.25) < tier 1 min (7.5)
        self.assertLess(sleeps[0], sleeps[1])

    def test_sleep_uses_time_sleep_not_asyncio(self):
        """Verify time.sleep is called (not asyncio.sleep)."""
        sleeps = []

        def fn(*a, **k):
            raise DownloadError("ERROR: HTTP Error 429")

        with patch.object(audio_player.time, "sleep", side_effect=sleeps.append):
            with self.assertRaises(DownloadError):
                _retry_with_backoff(fn, "q", "c", False, None, max_attempts=2, base_delay=5.0, jitter=0.0)

        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 5.0, places=5)

    def test_log_line_printed_on_retry(self):
        """Verify [retry] log line is printed for each retried attempt."""
        def fn(*a, **k):
            raise DownloadError("HTTP Error 429: Too Many Requests")

        printed = []
        with patch.object(audio_player.time, "sleep", lambda s: None):
            import builtins
            orig_print = builtins.print
            builtins.print = lambda *a, **k: printed.append(" ".join(str(x) for x in a))
            try:
                with self.assertRaises(DownloadError):
                    _retry_with_backoff(fn, "q", "c", False, None, max_attempts=3, base_delay=5.0, jitter=0.25)
            finally:
                builtins.print = orig_print

        retry_lines = [l for l in printed if l.startswith("[retry]")]
        # Two sleeps between 3 attempts → two [retry] log lines
        self.assertEqual(len(retry_lines), 2)
        self.assertIn("attempt 1/3", retry_lines[0])
        self.assertIn("attempt 2/3", retry_lines[1])
        self.assertIn("sleeping", retry_lines[0])


# ---------------------------------------------------------------------------
# get_audio_url_with_retry wrapper
# ---------------------------------------------------------------------------

class TestGetAudioUrlWithRetry(unittest.TestCase):

    def test_signature_matches_get_audio_url(self):
        """Wrapper must accept the same positional/keyword args as get_audio_url."""
        import inspect
        w_sig = inspect.signature(get_audio_url_with_retry)
        params = list(w_sig.parameters.keys())
        self.assertEqual(params, ["query", "client", "debug", "cookies_file"])

    def test_success_returns_dict_unchanged(self):
        fake_result = {"title": "Song", "url": "https://example.com/audio.m4a",
                       "http_headers": {}, "thumbnail": "", "webpage_url": "", "is_audio_only": True}
        with patch("audio_player.get_audio_url", return_value=fake_result) as mock_fn:
            with patch.object(audio_player.time, "sleep", lambda s: None):
                result = get_audio_url_with_retry("test query", "web")
        self.assertEqual(result, fake_result)
        mock_fn.assert_called_once()

    def test_non_retryable_raises_immediately(self):
        """Non-retryable DownloadError must surface on first call with no sleep."""
        sleeps = []

        def fake_get_audio_url(*a, **k):
            raise DownloadError("ERROR: Video unavailable")

        with patch("audio_player.get_audio_url", side_effect=fake_get_audio_url):
            with patch.object(audio_player.time, "sleep", side_effect=sleeps.append):
                with self.assertRaises(DownloadError):
                    get_audio_url_with_retry("test query", "web")

        self.assertEqual(len(sleeps), 0)

    def test_retryable_three_times_raises_after_three_calls(self):
        """Retryable error exhausts all 3 attempts then re-raises."""
        calls = []
        sleeps = []

        def fake_get_audio_url(*a, **k):
            calls.append(1)
            raise DownloadError("ERROR: HTTP Error 429: Too Many Requests")

        with patch("audio_player.get_audio_url", side_effect=fake_get_audio_url):
            with patch.object(audio_player.time, "sleep", side_effect=sleeps.append):
                with self.assertRaises(DownloadError):
                    get_audio_url_with_retry("test query", "web")

        self.assertEqual(len(calls), 3)
        self.assertEqual(len(sleeps), 2)

    def test_calls_retry_with_correct_params(self):
        """Wrapper must pass max_attempts=3, base_delay=5.0, jitter=0.25."""
        call_kwargs = {}

        def fake_retry(fn, *args, max_attempts, base_delay, jitter, **kwargs):
            call_kwargs["max_attempts"] = max_attempts
            call_kwargs["base_delay"] = base_delay
            call_kwargs["jitter"] = jitter
            return {"title": "t", "url": "u", "http_headers": {}, "thumbnail": "",
                    "webpage_url": "", "is_audio_only": True}

        with patch("audio_player._retry_with_backoff", side_effect=fake_retry):
            get_audio_url_with_retry("q", "web")

        self.assertEqual(call_kwargs["max_attempts"], 3)
        self.assertEqual(call_kwargs["base_delay"], 5.0)
        self.assertEqual(call_kwargs["jitter"], 0.25)


# ---------------------------------------------------------------------------
# Call-site verification (source-level)
# ---------------------------------------------------------------------------

_AUDIO_PLAYER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "audio_player.py"
)


class TestCallSite(unittest.TestCase):

    @classmethod
    def _read_source(cls) -> str:
        with open(_AUDIO_PLAYER_PATH, encoding="utf-8") as fh:
            return fh.read()

    def test_play_uses_get_audio_url_with_retry(self):
        src = self._read_source()
        self.assertIn("run_in_executor(None, get_audio_url_with_retry,", src,
                      "AudioPlayer.play() must use get_audio_url_with_retry, not get_audio_url")
        self.assertNotIn("run_in_executor(None, get_audio_url,", src,
                         "Old call site with bare get_audio_url must be removed")

    def test_no_retry_on_subprocess_path(self):
        src = self._read_source()
        self.assertNotIn("_start_ytdlp_stream_with_retry", src)
        self.assertNotIn("retry_with_backoff(_start_ytdlp_stream", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
