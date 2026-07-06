"""Tests for the aurora viewing-window forecast (network mocked; logic offline)."""
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aurora


class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestGetKpForecast(unittest.TestCase):
    def test_parses_and_sorts(self):
        payload = json.dumps([
            {"time_tag": "2026-01-15T21:00:00", "kp": 5.0, "observed": "predicted"},
            {"time_tag": "2026-01-15T18:00:00", "kp": 3.0, "observed": "predicted"},
        ]).encode()
        with patch("aurora.urllib.request.urlopen", return_value=_FakeResp(payload)):
            out = aurora.get_kp_forecast()
        self.assertEqual([kp for _, kp in out], [3.0, 5.0])  # sorted by time

    def test_error_returns_none(self):
        with patch("aurora.urllib.request.urlopen", side_effect=Exception("boom")):
            self.assertIsNone(aurora.get_kp_forecast())


class TestGeomag(unittest.TestCase):
    def test_riga_geomag_and_threshold(self):
        gm = aurora.geomag_lat(56.95, 24.11)
        self.assertTrue(50 < gm < 58)                      # Riga ≈ 55° geomagnetic
        self.assertTrue(4.0 < aurora.kp_needed(56.95, 24.11) < 6.0)  # needs Kp ~5

    def test_higher_latitude_needs_less_kp(self):
        tromso = aurora.kp_needed(69.6, 18.9)
        riga = aurora.kp_needed(56.95, 24.11)
        self.assertLess(tromso, riga)

    def test_southern_hemisphere_uses_magnitude(self):
        # A far-south location should need a LOW Kp (aurora australis), not a huge one.
        self.assertLess(aurora.kp_needed(-69.0, 18.0), 3.0)


class TestForecastLine(unittest.TestCase):
    def setUp(self):
        # Winter evening so darkness exists; high-latitude spot so a modest Kp qualifies.
        self.now = datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc)
        self.lat, self.lon = 69.6, 18.9  # Tromsø → kp_needed ~0
        self.kp = [(datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc), 3.0)]

    def test_best_window_when_dark_clear_active(self):
        sky = [("2026-01-15T20:00", 10, 0), ("2026-01-15T22:00", 90, 0)]
        line = aurora.forecast_line(self.kp, sky, self.lat, self.lon, tz_name=None, now=self.now)
        self.assertEqual(line, "Aurora: best ~20:00 (Kp 3)")  # clearest dark hour

    def test_cloudy_when_overcast(self):
        sky = [("2026-01-15T20:00", 95, 0)]
        line = aurora.forecast_line(self.kp, sky, self.lat, self.lon, tz_name=None, now=self.now)
        self.assertEqual(line, "Aurora: cloudy (Kp 3)")

    def test_no_darkness_omitted(self):
        sky = [("2026-01-15T20:00", 10, 1)]  # is_day=1 → daytime
        self.assertEqual(aurora.forecast_line(self.kp, sky, self.lat, self.lon, now=self.now), "")

    def test_kp_too_low_for_latitude_omitted(self):
        sky = [("2026-01-15T20:00", 10, 0)]
        # Low latitude needs a high Kp; Kp 3 is far too low → omit
        self.assertEqual(aurora.forecast_line(self.kp, sky, 40.0, -74.0, now=self.now), "")

    def test_empty_inputs(self):
        self.assertEqual(aurora.forecast_line(None, [("x", 0, 0)], 69, 18), "")
        self.assertEqual(aurora.forecast_line(self.kp, None, 69, 18), "")

    def test_timezone_localizes_time(self):
        sky = [("2026-01-15T20:00", 10, 0)]
        line = aurora.forecast_line(self.kp, sky, self.lat, self.lon, tz_name="Europe/Riga", now=self.now)
        self.assertIn("22:00", line)  # 20:00 UTC → 22:00 EET


if __name__ == "__main__":
    unittest.main(verbosity=2)
