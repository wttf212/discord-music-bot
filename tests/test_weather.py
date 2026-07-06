"""Tests for the weather helpers (network mocked)."""
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import weather


class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wx(temp, code):
    cur = {"temperature_2m": temp}
    if code is not None:
        cur["weather_code"] = code
    return json.dumps({"current": cur}).encode()


class TestGetWeather(unittest.TestCase):
    def test_known_code_with_label(self):
        with patch("weather.urllib.request.urlopen", return_value=_FakeResp(_wx(3.4, 61))):
            self.assertEqual(weather.get_weather(56.95, 24.11, "Riga"), "Riga 3°C, light rain")

    def test_negative_and_rounding(self):
        with patch("weather.urllib.request.urlopen", return_value=_FakeResp(_wx(-2.6, 71))):
            self.assertEqual(weather.get_weather(56.95, 24.11, "Riga"), "Riga -3°C, light snow")

    def test_unknown_code_omits_desc(self):
        with patch("weather.urllib.request.urlopen", return_value=_FakeResp(_wx(5.0, 999))):
            self.assertEqual(weather.get_weather(1, 2, "X"), "X 5°C")

    def test_missing_temp_returns_none(self):
        with patch("weather.urllib.request.urlopen", return_value=_FakeResp(_wx(None, 1))):
            self.assertIsNone(weather.get_weather(1, 2, "X"))

    def test_network_error_returns_none(self):
        with patch("weather.urllib.request.urlopen", side_effect=Exception("boom")):
            self.assertIsNone(weather.get_weather(1, 2, "X"))


class TestGeocode(unittest.TestCase):
    def test_found(self):
        payload = json.dumps({"results": [
            {"name": "Riga", "country_code": "LV", "latitude": 56.95, "longitude": 24.1}
        ]}).encode()
        with patch("weather.urllib.request.urlopen", return_value=_FakeResp(payload)):
            self.assertEqual(weather.geocode("riga"), ("Riga, LV", 56.95, 24.1))

    def test_not_found(self):
        with patch("weather.urllib.request.urlopen", return_value=_FakeResp(json.dumps({"results": []}).encode())):
            self.assertIsNone(weather.geocode("zzznowhere"))

    def test_error(self):
        with patch("weather.urllib.request.urlopen", side_effect=Exception("boom")):
            self.assertIsNone(weather.geocode("x"))


class TestHourlySky(unittest.TestCase):
    def test_parses_hourly(self):
        payload = json.dumps({"hourly": {
            "time": ["2026-01-15T18:00", "2026-01-15T19:00"],
            "cloud_cover": [10, 90],
            "is_day": [0, 1],
        }}).encode()
        with patch("weather.urllib.request.urlopen", return_value=_FakeResp(payload)):
            out = weather.get_hourly_sky(56.95, 24.11)
        self.assertEqual(out, [("2026-01-15T18:00", 10, 0), ("2026-01-15T19:00", 90, 1)])

    def test_error_returns_none(self):
        with patch("weather.urllib.request.urlopen", side_effect=Exception("boom")):
            self.assertIsNone(weather.get_hourly_sky(1, 2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
