"""Tests for the next-rocket-launch helpers (network mocked; formatting offline)."""
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rocket


class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _feed(results):
    return json.dumps({"results": results}).encode()


class TestGetNextLaunch(unittest.TestCase):
    def test_parses_and_shortens_name(self):
        results = [{"name": "Falcon 9 Block 5 | Starlink 12-5", "net": "2026-07-07T07:10:00Z"}]
        with patch("rocket.urllib.request.urlopen", return_value=_FakeResp(_feed(results))):
            r = rocket.get_next_launch()
        self.assertEqual(r["name"], "Falcon 9 Block 5 – Starlink 12-5")   # " | " -> " – "
        self.assertEqual(r["dt"], "2026-07-07T07:10:00+00:00")

    def test_truncates_long_name(self):
        results = [{"name": "X" * 80, "net": "2026-07-07T07:10:00Z"}]
        with patch("rocket.urllib.request.urlopen", return_value=_FakeResp(_feed(results))):
            r = rocket.get_next_launch()
        self.assertLessEqual(len(r["name"]), 48)
        self.assertTrue(r["name"].endswith("…"))

    def test_empty_returns_none(self):
        with patch("rocket.urllib.request.urlopen", return_value=_FakeResp(_feed([]))):
            self.assertIsNone(rocket.get_next_launch())

    def test_network_error(self):
        with patch("rocket.urllib.request.urlopen", side_effect=Exception("boom")):
            self.assertIsNone(rocket.get_next_launch())


class TestFormatLaunch(unittest.TestCase):
    def test_countdown_days(self):
        launch = {"name": "Falcon 9 – Starlink", "dt": "2026-07-19T13:00:00+00:00"}
        now = datetime(2026, 7, 17, 13, 0, tzinfo=timezone.utc)
        self.assertEqual(rocket.format_launch(launch, now=now), "Next space flight: Falcon 9 – Starlink (in 2d)")

    def test_countdown_hours(self):
        launch = {"name": "Falcon 9", "dt": "2026-07-19T13:00:00+00:00"}
        now = datetime(2026, 7, 19, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(rocket.format_launch(launch, now=now), "Next space flight: Falcon 9 (in 4h)")

    def test_none(self):
        self.assertEqual(rocket.format_launch(None), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
