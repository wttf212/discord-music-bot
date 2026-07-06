"""Tests for the Riga weather footer helper (network mocked)."""
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


def _payload(temp, code):
    cur = {"temperature_2m": temp}
    if code is not None:
        cur["weather_code"] = code
    return json.dumps({"current": cur}).encode()


class TestGetRigaWeather(unittest.TestCase):
    def test_known_code(self):
        with patch("weather.urllib.request.urlopen", return_value=_FakeResp(_payload(3.4, 61))):
            self.assertEqual(weather.get_riga_weather(), "Riga 3°C, light rain")

    def test_rounds_and_handles_negative(self):
        with patch("weather.urllib.request.urlopen", return_value=_FakeResp(_payload(-2.6, 71))):
            self.assertEqual(weather.get_riga_weather(), "Riga -3°C, light snow")

    def test_unknown_code_omits_desc(self):
        with patch("weather.urllib.request.urlopen", return_value=_FakeResp(_payload(5.0, 999))):
            self.assertEqual(weather.get_riga_weather(), "Riga 5°C")

    def test_missing_temp_returns_none(self):
        with patch("weather.urllib.request.urlopen", return_value=_FakeResp(_payload(None, 1))):
            self.assertIsNone(weather.get_riga_weather())

    def test_network_error_returns_none(self):
        with patch("weather.urllib.request.urlopen", side_effect=Exception("boom")):
            self.assertIsNone(weather.get_riga_weather())


if __name__ == "__main__":
    unittest.main(verbosity=2)
