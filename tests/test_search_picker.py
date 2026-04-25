"""Tests for Phase 08 search picker helpers (08-01).

These tests exercise commands._search_youtube, _fmt_duration, _is_search_query,
_strip_ytsearch_prefix, and _build_search_embed WITHOUT hitting the network.
"""
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import commands


class TestFormatDuration(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(commands._fmt_duration(225), "3:45")

    def test_minute_boundary(self):
        self.assertEqual(commands._fmt_duration(60), "1:00")

    def test_single_digit_seconds_padded(self):
        self.assertEqual(commands._fmt_duration(5), "0:05")

    def test_zero(self):
        self.assertEqual(commands._fmt_duration(0), "0:00")

    def test_none_returns_question_mark(self):
        self.assertEqual(commands._fmt_duration(None), "?")

    def test_float_truncates(self):
        self.assertEqual(commands._fmt_duration(225.7), "3:45")


class TestIsSearchQuery(unittest.TestCase):
    def test_plain_text_is_search(self):
        self.assertTrue(commands._is_search_query("jazz music"))

    def test_https_url_bypasses(self):
        self.assertFalse(commands._is_search_query("https://youtu.be/abc"))

    def test_http_url_bypasses(self):
        self.assertFalse(commands._is_search_query("http://example.com/x"))

    def test_uppercase_url_bypasses(self):
        self.assertFalse(commands._is_search_query("HTTPS://Y.COM"))

    def test_ytsearch_prefix_is_search(self):
        self.assertTrue(commands._is_search_query("ytsearch:jazz"))

    def test_empty_string_not_search(self):
        self.assertFalse(commands._is_search_query(""))


class TestStripYtsearchPrefix(unittest.TestCase):
    def test_strips_bare_prefix(self):
        self.assertEqual(commands._strip_ytsearch_prefix("ytsearch:jazz music"), "jazz music")

    def test_does_not_strip_ytsearch5(self):
        self.assertEqual(commands._strip_ytsearch_prefix("ytsearch5:jazz"), "ytsearch5:jazz")

    def test_passthrough_plain(self):
        self.assertEqual(commands._strip_ytsearch_prefix("plain text"), "plain text")

    def test_empty(self):
        self.assertEqual(commands._strip_ytsearch_prefix(""), "")


class TestSearchYoutube(unittest.TestCase):
    def _mock_ydl(self, entries):
        """Return a MagicMock that behaves like yt_dlp.YoutubeDL(opts) context manager."""
        cm = MagicMock()
        cm.extract_info.return_value = {"entries": entries}
        ydl_class = MagicMock()
        ydl_class.return_value.__enter__.return_value = cm
        ydl_class.return_value.__exit__.return_value = False
        return ydl_class, cm

    def test_returns_five_normalized_results(self):
        entries = [
            {"title": f"T{i}", "url": f"https://yt/{i}", "uploader": f"U{i}",
             "duration": 60 + i, "thumbnails": [{"url": f"https://t/{i}"}]}
            for i in range(5)
        ]
        ydl_class, cm = self._mock_ydl(entries)
        with patch("commands.yt_dlp.YoutubeDL", ydl_class):
            out = commands._search_youtube("jazz")
        self.assertEqual(len(out), 5)
        self.assertEqual(out[0]["title"], "T0")
        self.assertEqual(out[0]["url"], "https://yt/0")
        self.assertEqual(out[0]["uploader"], "U0")
        self.assertEqual(out[0]["duration_str"], "1:00")
        self.assertEqual(out[0]["thumbnail"], "https://t/0")

    def test_extract_info_called_with_ytsearch5_prefix(self):
        ydl_class, cm = self._mock_ydl([])
        with patch("commands.yt_dlp.YoutubeDL", ydl_class):
            commands._search_youtube("jazz music")
        cm.extract_info.assert_called_once_with("ytsearch5:jazz music", download=False)

    def test_uses_extract_flat_in_playlist(self):
        ydl_class, _ = self._mock_ydl([])
        with patch("commands.yt_dlp.YoutubeDL", ydl_class):
            commands._search_youtube("anything")
        opts = ydl_class.call_args[0][0]
        self.assertEqual(opts.get("extract_flat"), "in_playlist")
        self.assertTrue(opts.get("quiet"))

    def test_missing_duration_yields_question_mark(self):
        ydl_class, _ = self._mock_ydl([{"title": "X", "url": "u", "uploader": "U", "thumbnails": []}])
        with patch("commands.yt_dlp.YoutubeDL", ydl_class):
            out = commands._search_youtube("q")
        self.assertEqual(out[0]["duration_str"], "?")
        self.assertEqual(out[0]["thumbnail"], "")

    def test_missing_uploader_falls_back_to_channel_then_unknown(self):
        entries = [
            {"title": "A", "url": "u", "channel": "Chan", "duration": 30, "thumbnails": []},
            {"title": "B", "url": "u", "duration": 30, "thumbnails": []},
        ]
        ydl_class, _ = self._mock_ydl(entries)
        with patch("commands.yt_dlp.YoutubeDL", ydl_class):
            out = commands._search_youtube("q")
        self.assertEqual(out[0]["uploader"], "Chan")
        self.assertEqual(out[1]["uploader"], "Unknown")

    def test_empty_entries_returns_empty_list(self):
        ydl_class, _ = self._mock_ydl([])
        with patch("commands.yt_dlp.YoutubeDL", ydl_class):
            self.assertEqual(commands._search_youtube("q"), [])


class TestBuildSearchEmbed(unittest.TestCase):
    def _sample(self, n=3):
        return [
            {"title": f"Title {i}", "url": f"https://yt/{i}",
             "uploader": f"Chan{i}", "duration_str": f"{i}:00", "thumbnail": ""}
            for i in range(1, n + 1)
        ]

    def test_title_contains_query(self):
        embed = commands._build_search_embed("jazz music", self._sample(3))
        self.assertIn("jazz music", embed.title)
        self.assertIn("Results for", embed.title)

    def test_color_matches_np_embed(self):
        embed = commands._build_search_embed("q", self._sample(1))
        self.assertEqual(embed.color.value, 0x3498db)

    def test_description_has_numbering_and_meta(self):
        embed = commands._build_search_embed("q", self._sample(3))
        self.assertIn("**1.**", embed.description)
        self.assertIn("**2.**", embed.description)
        self.assertIn("**3.**", embed.description)
        self.assertIn("Chan1", embed.description)
        self.assertIn("1:00", embed.description)

    def test_long_query_truncated_to_50(self):
        long_q = "x" * 80
        embed = commands._build_search_embed(long_q, self._sample(1))
        # title is `🔍 Results for "xxxx..."` — query portion must be <=50 chars
        self.assertIn("x" * 50, embed.title)
        self.assertNotIn("x" * 51, embed.title)

    def test_footer_text(self):
        embed = commands._build_search_embed("q", self._sample(1))
        self.assertEqual(embed.footer.text, "Select a result below • Expires in 60s")


class TestSearchPickerView(unittest.TestCase):
    """Unit tests for SearchPickerView construction and Select option limits."""

    def _make_results(self, n=5):
        return [
            {
                "title": f"Track {i}",
                "url": f"https://www.youtube.com/watch?v=id{i}",
                "uploader": f"Channel {i}",
                "duration_str": f"{i}:00",
                "thumbnail": "",
            }
            for i in range(1, n + 1)
        ]

    def _make_view(self, n=5):
        bot = MagicMock()
        ctx = MagicMock()
        status_msg = MagicMock()
        return commands.SearchPickerView(bot, ctx, self._make_results(n), status_msg)

    def test_view_has_one_select_item(self):
        view = self._make_view(5)
        self.assertEqual(len(view.children), 1)

    def test_select_has_correct_option_count(self):
        view = self._make_view(5)
        select = view.children[0]
        self.assertEqual(len(select.options), 5)

    def test_select_option_label_is_title(self):
        view = self._make_view(3)
        self.assertEqual(view.children[0].options[0].label, "Track 1")

    def test_select_option_value_is_url(self):
        view = self._make_view(3)
        self.assertEqual(view.children[0].options[0].value, "https://www.youtube.com/watch?v=id1")

    def test_select_option_description_format(self):
        view = self._make_view(3)
        self.assertEqual(view.children[0].options[0].description, "Channel 1 • 1:00")

    def test_label_truncated_to_100_chars(self):
        results = [{"title": "A" * 150, "url": "https://yt/x",
                    "uploader": "U", "duration_str": "1:00", "thumbnail": ""}]
        bot, ctx, msg = MagicMock(), MagicMock(), MagicMock()
        view = commands.SearchPickerView(bot, ctx, results, msg)
        self.assertEqual(len(view.children[0].options[0].label), 100)

    def test_description_truncated_to_100_chars(self):
        results = [{"title": "T", "url": "https://yt/x",
                    "uploader": "C" * 100, "duration_str": "1:00", "thumbnail": ""}]
        bot, ctx, msg = MagicMock(), MagicMock(), MagicMock()
        view = commands.SearchPickerView(bot, ctx, results, msg)
        self.assertLessEqual(len(view.children[0].options[0].description), 100)

    def test_view_timeout_is_60(self):
        view = self._make_view(1)
        self.assertEqual(view.timeout, 60)

    def test_single_result_still_shows_picker(self):
        """One result still shows the select — no auto-play for single result per spec."""
        view = self._make_view(1)
        self.assertEqual(len(view.children[0].options), 1)


class TestSearchPickerIntegration(unittest.TestCase):
    """Integration tests for URL bypass (no picker) and no-results path.

    These tests exercise _is_search_query routing logic only — the full
    picker flow (interaction, playback) requires a live Discord session
    and is covered by manual verification per 08-VALIDATION.md.
    """

    def test_url_query_bypasses_picker(self):
        """HTTP(S) URLs are not search queries — picker must not run."""
        self.assertFalse(commands._is_search_query("https://youtu.be/dQw4w9WgXcQ"))
        self.assertFalse(commands._is_search_query("http://youtu.be/dQw4w9WgXcQ"))

    def test_plain_text_triggers_picker(self):
        self.assertTrue(commands._is_search_query("bohemian rhapsody"))

    def test_ytsearch_prefix_triggers_picker_and_is_stripped(self):
        query = "ytsearch:never gonna give you up"
        self.assertTrue(commands._is_search_query(query))
        self.assertEqual(commands._strip_ytsearch_prefix(query), "never gonna give you up")

    def test_search_returns_empty_on_mock_no_results(self):
        """When yt-dlp returns 0 entries, _search_youtube returns empty list."""
        cm = MagicMock()
        cm.extract_info.return_value = {"entries": []}
        ydl_class = MagicMock()
        ydl_class.return_value.__enter__.return_value = cm
        ydl_class.return_value.__exit__.return_value = False
        with patch("commands.yt_dlp.YoutubeDL", ydl_class):
            result = commands._search_youtube("zzz_no_results_query_xyz")
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
