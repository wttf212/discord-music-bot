"""Tests for the F1 next-race helpers (network mocked; formatting is offline)."""
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import f1


class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _feed(races):
    return json.dumps({"MRData": {"RaceTable": {"Races": races}}}).encode()


class TestGetNextRace(unittest.TestCase):
    def test_parses_race(self):
        races = [{
            "raceName": "Belgian Grand Prix",
            "Circuit": {"circuitName": "Spa"},
            "date": "2026-07-19", "time": "13:00:00Z",
        }]
        with patch("f1.urllib.request.urlopen", return_value=_FakeResp(_feed(races))):
            r = f1.get_next_race()
        self.assertEqual(r["name"], "Belgian GP")           # "Grand Prix" -> "GP"
        self.assertEqual(r["dt"], "2026-07-19T13:00:00+00:00")

    def test_no_time_defaults_midnight(self):
        races = [{"raceName": "X GP", "Circuit": {}, "date": "2026-03-01"}]
        with patch("f1.urllib.request.urlopen", return_value=_FakeResp(_feed(races))):
            r = f1.get_next_race()
        self.assertEqual(r["dt"], "2026-03-01T00:00:00+00:00")

    def test_empty_offseason(self):
        with patch("f1.urllib.request.urlopen", return_value=_FakeResp(_feed([]))):
            self.assertIsNone(f1.get_next_race())

    def test_network_error(self):
        with patch("f1.urllib.request.urlopen", side_effect=Exception("boom")):
            self.assertIsNone(f1.get_next_race())


class TestFormatRace(unittest.TestCase):
    def setUp(self):
        self.race = {"name": "Belgian GP", "circuit": "Spa", "dt": "2026-07-19T13:00:00+00:00"}
        self.now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)  # 13 days before

    def test_localized_and_countdown(self):
        s = f1.format_race(self.race, "Europe/Riga", now=self.now)
        self.assertTrue(s.startswith("F1: Belgian GP "))
        self.assertIn("16:00", s)      # 13:00 UTC -> 16:00 EEST
        self.assertIn("(in 13d)", s)
        self.assertNotIn("UTC", s)     # localized, no UTC suffix

    def test_unknown_tz_falls_back_to_utc(self):
        s = f1.format_race(self.race, "Not/AZone", now=self.now)
        self.assertIn("13:00 UTC", s)

    def test_none_race(self):
        self.assertEqual(f1.format_race(None, "Europe/Riga"), "")

    def test_hours_countdown(self):
        now = datetime(2026, 7, 19, 9, 0, tzinfo=timezone.utc)  # 4h before
        s = f1.format_race(self.race, "Europe/Riga", now=now)
        self.assertIn("(in 4h)", s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
