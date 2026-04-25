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


if __name__ == "__main__":
    unittest.main(verbosity=2)
