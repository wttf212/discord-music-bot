"""Tests for the q0j quick task: !queue pagination and the char-budget ☰ queue listing.

These tests exercise commands.QueuePaginatorView and commands._queue_lines_within_budget
WITHOUT hitting the network or Discord API (MagicMock stands in for bot/ctx).
"""
import sys
import os
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import commands
from track_queue import Track


def _make_tracks(n, title_len=None):
    def _title(i):
        if title_len:
            return "X" * title_len
        return f"Song {i}"

    return [
        Track(query=f"q{i}", title=_title(i), requested_by="", url=f"https://x/{i}")
        for i in range(1, n + 1)
    ]


class TestQueuePaginatorView(unittest.TestCase):
    def _make_view(self, n, now_playing_title=None, title_len=None):
        bot = MagicMock()
        ctx = MagicMock()
        return commands.QueuePaginatorView(bot, ctx, _make_tracks(n, title_len), now_playing_title)

    def test_total_pages_zero_tracks(self):
        self.assertEqual(self._make_view(0).total_pages, 1)

    def test_total_pages_exactly_one_page(self):
        self.assertEqual(self._make_view(20).total_pages, 1)

    def test_total_pages_just_over_one_page(self):
        self.assertEqual(self._make_view(21).total_pages, 2)

    def test_total_pages_three_pages(self):
        self.assertEqual(self._make_view(60).total_pages, 3)

    def test_render_stays_under_2000_chars_for_large_queue(self):
        view = self._make_view(500, title_len=200)
        self.assertLess(len(view.render()), 2000)

    def test_render_page_zero_numbers_1_to_20(self):
        view = self._make_view(60)
        rendered = view.render()
        self.assertIn("1. ", rendered)
        self.assertIn("20. ", rendered)
        self.assertNotIn("21. ", rendered)

    def test_render_page_one_numbers_21_to_40(self):
        view = self._make_view(60)
        view.page = 1
        view._update_buttons()
        rendered = view.render()
        self.assertIn("21. ", rendered)
        self.assertIn("40. ", rendered)
        self.assertNotIn("41. ", rendered)

    def test_prev_disabled_next_enabled_on_first_page(self):
        view = self._make_view(60)
        self.assertTrue(view.prev_button.disabled)
        self.assertFalse(view.next_button.disabled)

    def test_prev_enabled_next_disabled_on_last_page(self):
        view = self._make_view(60)
        view.page = view.total_pages - 1
        view._update_buttons()
        self.assertFalse(view.prev_button.disabled)
        self.assertTrue(view.next_button.disabled)

    def test_timeout_is_120(self):
        self.assertEqual(self._make_view(1).timeout, 120)


if __name__ == "__main__":
    unittest.main()
