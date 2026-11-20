"""Tests for zero-result YouTube searches.

YouTube age-gates some result sets: an anonymous search gets a "Confirm your age"
panel and estimatedResults: 0 instead of hits. That used to reach
`info["entries"][0]` and surface as a bare `IndexError: list index out of range`,
which the classifier then read as an ambiguous (retryable) error and paced on the
slow rate-limit tier — ~15s of dead air for an outcome that can never change.

These pin the three behaviours that replaced it: a descriptive error, non-retryable
classification, and a single "Artist - Title" -> "Title" fallback search.
No network: YoutubeDL is faked throughout.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_player
from audio_player import (
    _drop_artist_prefix,
    _is_retryable_ytdlp_error,
    _no_results_error,
    _resolve_audio_url,
    _retry_with_backoff,
    is_permanent_resolve_error,
)


def _hit(title="A Song"):
    """A minimal resolved-entry dict, shaped like what yt-dlp returns."""
    return {
        "url": "https://rr1---sn-x.googlevideo.com/videoplayback?expire=99999999999",
        "title": title,
        "thumbnail": "",
        "webpage_url": "https://youtu.be/abc",
        "vcodec": "none",
        "duration": 100,
    }


class FakeYDL:
    """Records every extract_info query and replays a scripted response per call."""

    queries: list = []
    responses: list = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass

    def extract_info(self, query, download=False):
        FakeYDL.queries.append(query)
        idx = len(FakeYDL.queries) - 1
        return FakeYDL.responses[min(idx, len(FakeYDL.responses) - 1)]


class ResolveTestCase(unittest.TestCase):
    def setUp(self):
        audio_player._resolvers.clear()
        FakeYDL.queries = []
        FakeYDL.responses = [{"entries": []}]
        self._patcher = patch.object(audio_player, "YoutubeDL", FakeYDL)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        audio_player._resolvers.clear()


# ---------------------------------------------------------------------------
# The guard: descriptive error, never IndexError
# ---------------------------------------------------------------------------

class TestEmptyEntriesGuard(ResolveTestCase):

    def test_empty_entries_does_not_raise_indexerror(self):
        with self.assertRaises(Exception) as ctx:
            _resolve_audio_url("nothing matches this", "web_embedded")
        self.assertNotIsInstance(ctx.exception, IndexError)

    def test_error_names_the_query_and_the_age_gate(self):
        with self.assertRaises(RuntimeError) as ctx:
            _resolve_audio_url("nothing matches this", "web_embedded")
        msg = str(ctx.exception)
        self.assertIn("nothing matches this", msg)
        self.assertIn("age-restricted", msg)
        self.assertNotIn("list index out of range", msg)

    def test_populated_entries_still_takes_the_first(self):
        FakeYDL.responses = [{"entries": [_hit("First"), _hit("Second")]}]
        result = _resolve_audio_url("a real query", "web_embedded")
        self.assertEqual(result["title"], "First")

    def test_entries_none_is_treated_as_empty(self):
        FakeYDL.responses = [{"entries": None}]
        with self.assertRaises(RuntimeError):
            _resolve_audio_url("no dash here", "web_embedded")


# ---------------------------------------------------------------------------
# Classification: must not burn backoff on a fixed outcome
# ---------------------------------------------------------------------------

class TestNonRetryable(unittest.TestCase):

    def test_no_results_error_is_not_retryable(self):
        self.assertFalse(_is_retryable_ytdlp_error(_no_results_error("some query")))

    def test_no_results_error_is_permanent(self):
        # Permanent => the prefetch drops the track instead of re-resolving it.
        self.assertTrue(is_permanent_resolve_error(_no_results_error("some query")))

    def test_retry_wrapper_does_not_sleep_and_calls_once(self):
        calls = []

        def boom():
            calls.append(1)
            raise _no_results_error("some query")

        with patch.object(audio_player.time, "sleep") as slept:
            with self.assertRaises(RuntimeError):
                _retry_with_backoff(boom, max_attempts=3, base_delay=5.0)
        self.assertEqual(len(calls), 1, "a zero-result search must not be retried")
        slept.assert_not_called()


# ---------------------------------------------------------------------------
# Recovery: one trimmed-query fallback
# ---------------------------------------------------------------------------

class TestArtistPrefixHelper(unittest.TestCase):

    def test_drops_leading_artist(self):
        self.assertEqual(
            _drop_artist_prefix("Milky Chance - Flashed Junk Mind"),
            "Flashed Junk Mind",
        )

    def test_splits_on_the_first_separator_only(self):
        self.assertEqual(_drop_artist_prefix("A - B - C"), "B - C")

    def test_no_separator_returns_none(self):
        self.assertIsNone(_drop_artist_prefix("Flashed Junk Mind"))

    def test_hyphen_without_spaces_is_not_a_separator(self):
        self.assertIsNone(_drop_artist_prefix("Jay-Z Encore"))

    def test_trailing_separator_returns_none(self):
        self.assertIsNone(_drop_artist_prefix("Artist - "))


class TestTrimmedFallback(ResolveTestCase):

    def test_zero_results_retries_once_with_artist_dropped(self):
        FakeYDL.responses = [{"entries": []}, {"entries": [_hit("Flashed Junk Mind")]}]
        result = _resolve_audio_url("Milky Chance - Flashed Junk Mind", "web_embedded")
        self.assertEqual(result["title"], "Flashed Junk Mind")
        self.assertEqual(FakeYDL.queries, [
            "ytsearch:Milky Chance - Flashed Junk Mind",
            "ytsearch:Flashed Junk Mind",
        ])

    def test_successful_search_never_triggers_the_fallback(self):
        FakeYDL.responses = [{"entries": [_hit("Stolen Dance")]}]
        _resolve_audio_url("Milky Chance - Stolen Dance", "web_embedded")
        self.assertEqual(len(FakeYDL.queries), 1, "no extra search on success")

    def test_fallback_fires_at_most_once(self):
        FakeYDL.responses = [{"entries": []}]  # every search comes back empty
        with self.assertRaises(RuntimeError):
            _resolve_audio_url("A - B - C", "web_embedded")
        self.assertEqual(FakeYDL.queries, ["ytsearch:A - B - C", "ytsearch:B - C"])

    def test_exhausted_fallback_reports_the_original_query(self):
        FakeYDL.responses = [{"entries": []}]
        with self.assertRaises(RuntimeError) as ctx:
            _resolve_audio_url("Milky Chance - Flashed Junk Mind", "web_embedded")
        msg = str(ctx.exception)
        self.assertIn("Milky Chance - Flashed Junk Mind", msg)

    def test_no_fallback_without_a_separator(self):
        FakeYDL.responses = [{"entries": []}]
        with self.assertRaises(RuntimeError):
            _resolve_audio_url("Flashed Junk Mind", "web_embedded")
        self.assertEqual(len(FakeYDL.queries), 1)

    def test_url_query_never_gets_the_fallback(self):
        FakeYDL.responses = [{"entries": []}]
        url = "https://www.youtube.com/playlist?list=PL_a - b"
        with self.assertRaises(RuntimeError):
            _resolve_audio_url(url, "web_embedded")
        self.assertEqual(FakeYDL.queries, [url], "a URL has no search to redo")


if __name__ == "__main__":
    unittest.main()
