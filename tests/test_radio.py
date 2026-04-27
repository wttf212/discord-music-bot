"""Tests for Phase 09 radio helpers and RadioPickerView.

Exercises _fetch_radio_stations, _build_radio_embed, and RadioPickerView construction
WITHOUT hitting the network. All HTTP calls are mocked.
"""
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import commands


SAMPLE_STATIONS = [
    {
        "name": f"Station {i}",
        "url_resolved": f"http://stream{i}.example.com/live",
        "favicon": f"http://icon{i}.example.com/favicon.ico",
        "tags": "Jazz,Soul",
        "country": "US",
        "bitrate": 128,
    }
    for i in range(15)
]


class TestRadioHelpers(unittest.TestCase):
    """Tests for _build_radio_embed helper (network-free)."""

    def test_title_no_query(self):
        embed = commands._build_radio_embed(SAMPLE_STATIONS[:5], None, 1, 3)
        self.assertIn("Radio Stations", embed.title)

    def test_title_with_query(self):
        embed = commands._build_radio_embed(SAMPLE_STATIONS[:5], "jazz", 2, 5)
        self.assertIn("Results for", embed.title)
        self.assertIn("jazz", embed.title)

    def test_footer_contains_page_info(self):
        embed = commands._build_radio_embed(SAMPLE_STATIONS[:5], None, 1, 3)
        self.assertIn("Page 1 of 3", embed.footer.text)

    def test_footer_contains_attribution(self):
        embed = commands._build_radio_embed(SAMPLE_STATIONS[:5], None, 1, 3)
        self.assertIn("radio-browser.info", embed.footer.text)

    def test_color_matches_search_embed(self):
        embed = commands._build_radio_embed(SAMPLE_STATIONS[:3], None, 1, 1)
        self.assertEqual(embed.color.value, 0x3498db)

    def test_description_contains_station_name(self):
        embed = commands._build_radio_embed(SAMPLE_STATIONS[:1], None, 1, 1)
        self.assertIn("Station 0", embed.description)

    def test_long_query_truncated(self):
        long_q = "x" * 80
        embed = commands._build_radio_embed(SAMPLE_STATIONS[:1], long_q, 1, 1)
        self.assertNotIn("x" * 41, embed.title)

    def test_empty_stations_shows_no_stations_found(self):
        embed = commands._build_radio_embed([], None, 1, 1)
        self.assertIn("No stations found", embed.description)


class TestFetchRadioStations(unittest.TestCase):
    """Tests for _fetch_radio_stations helper (urllib mocked)."""

    def _mock_urlopen(self, data: list):
        import json
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(data).encode()
        mock_resp.__enter__ = lambda self: self
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_topvote_url_used_when_no_query(self):
        with patch("commands.urllib.request.urlopen") as mock_urlopen, \
             patch("commands.urllib.request.Request") as mock_req:
            mock_urlopen.return_value = self._mock_urlopen([])
            commands._fetch_radio_stations(None)
        call_args = str(mock_req.call_args)
        self.assertIn("topvote", call_args)

    def test_byname_url_used_when_query_given(self):
        with patch("commands.urllib.request.urlopen") as mock_urlopen, \
             patch("commands.urllib.request.Request") as mock_req:
            mock_urlopen.return_value = self._mock_urlopen([])
            commands._fetch_radio_stations("jazz")
        call_args = str(mock_req.call_args)
        self.assertIn("byname", call_args)
        self.assertIn("jazz", call_args)

    def test_user_agent_header_present(self):
        with patch("commands.urllib.request.urlopen") as mock_urlopen, \
             patch("commands.urllib.request.Request") as mock_req:
            mock_urlopen.return_value = self._mock_urlopen([])
            commands._fetch_radio_stations(None)
        call_repr = str(mock_req.call_args)
        self.assertIn("User-Agent", call_repr)

    def test_returns_parsed_json(self):
        with patch("commands.urllib.request.urlopen") as mock_urlopen, \
             patch("commands.urllib.request.Request"):
            mock_urlopen.return_value = self._mock_urlopen(SAMPLE_STATIONS[:5])
            result = commands._fetch_radio_stations(None)
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0]["name"], "Station 0")

    def test_search_url_used_when_country_given(self):
        with patch("commands.urllib.request.urlopen") as mock_urlopen, \
             patch("commands.urllib.request.Request") as mock_req:
            mock_urlopen.return_value = self._mock_urlopen([])
            commands._fetch_radio_stations(None, country="US")
        call_args = str(mock_req.call_args)
        self.assertIn("search", call_args)
        self.assertIn("countrycode", call_args)
        self.assertIn("US", call_args)

    def test_search_url_used_when_genre_given(self):
        with patch("commands.urllib.request.urlopen") as mock_urlopen, \
             patch("commands.urllib.request.Request") as mock_req:
            mock_urlopen.return_value = self._mock_urlopen([])
            commands._fetch_radio_stations(None, genre="jazz")
        call_args = str(mock_req.call_args)
        self.assertIn("search", call_args)
        self.assertIn("tagList", call_args)
        self.assertIn("jazz", call_args)


class TestRadioPickerView(unittest.TestCase):
    """Tests for RadioPickerView construction and pagination controls."""

    def _make_view(self, n=15, query=None):
        bot = MagicMock()
        ctx = MagicMock()
        msg = MagicMock()
        return commands.RadioPickerView(bot, ctx, SAMPLE_STATIONS[:n], msg, query=query)

    def test_view_has_three_items(self):
        """Select + Prev + Next = 3 children."""
        view = self._make_view(15)
        self.assertEqual(len(view.children), 3)

    def test_select_has_page_size_options(self):
        """First page shows PAGE_SIZE (10) options for 15 stations."""
        view = self._make_view(15)
        select = view.children[0]
        self.assertEqual(len(select.options), 10)

    def test_select_fewer_stations_than_page(self):
        """3 stations -> 3 select options, still 3 total children."""
        view = self._make_view(3)
        self.assertEqual(len(view.children[0].options), 3)

    def test_prev_button_disabled_on_first_page(self):
        view = self._make_view(15)
        prev_btn = view.children[1]
        self.assertTrue(prev_btn.disabled)

    def test_next_button_enabled_when_more_pages(self):
        view = self._make_view(15)
        next_btn = view.children[2]
        self.assertFalse(next_btn.disabled)

    def test_next_button_disabled_on_last_page(self):
        """3 stations / PAGE_SIZE=10 = 1 page. Next must be disabled."""
        view = self._make_view(3)
        next_btn = view.children[2]
        self.assertTrue(next_btn.disabled)

    def test_select_label_is_station_name(self):
        view = self._make_view(5)
        self.assertEqual(view.children[0].options[0].label, "Station 0")

    def test_select_value_is_stream_url(self):
        view = self._make_view(5)
        self.assertEqual(view.children[0].options[0].value, "http://stream0.example.com/live")

    def test_select_description_contains_bitrate(self):
        view = self._make_view(5)
        self.assertIn("128kbps", view.children[0].options[0].description)

    def test_select_description_truncated_to_100(self):
        long_stations = [dict(s) for s in SAMPLE_STATIONS[:1]]
        long_stations[0]["country"] = "X" * 90
        view = commands.RadioPickerView(MagicMock(), MagicMock(), long_stations, MagicMock())
        self.assertLessEqual(len(view.children[0].options[0].description), 100)

    def test_view_timeout_is_60(self):
        view = self._make_view(1)
        self.assertEqual(view.timeout, 60)

    def test_total_pages_calculation(self):
        view = self._make_view(15)  # 15 stations / 10 per page = 2 pages
        self.assertEqual(view._total_pages, 2)


class TestRadioDiscoveryView(unittest.TestCase):
    """Tests for RadioDiscoveryView construction."""

    def _make_view(self):
        bot = MagicMock()
        ctx = MagicMock()
        msg = MagicMock()
        return commands.RadioDiscoveryView(bot, ctx, msg)

    def test_child_count(self):
        view = self._make_view()
        self.assertEqual(len(view.children), 3)

    def test_country_select_option_count(self):
        view = self._make_view()
        self.assertEqual(len(view.children[0].options), 20)

    def test_genre_select_option_count(self):
        view = self._make_view()
        self.assertEqual(len(view.children[1].options), 16)

    def test_browse_button_style(self):
        view = self._make_view()
        import discord as _discord
        self.assertEqual(view.children[2].style, _discord.ButtonStyle.primary)

    def test_timeout_is_60(self):
        view = self._make_view()
        self.assertEqual(view.timeout, 60)

    def test_default_country_empty(self):
        view = self._make_view()
        self.assertEqual(view.country, "")

    def test_default_genre_empty(self):
        view = self._make_view()
        self.assertEqual(view.genre, "")

    def test_first_country_option_is_any(self):
        view = self._make_view()
        self.assertEqual(view.children[0].options[0].value, "")
        self.assertEqual(view.children[0].options[0].label, "Any country")

    def test_first_genre_option_is_any(self):
        view = self._make_view()
        self.assertEqual(view.children[1].options[0].value, "")
        self.assertEqual(view.children[1].options[0].label, "Any genre")


if __name__ == "__main__":
    unittest.main(verbosity=2)
