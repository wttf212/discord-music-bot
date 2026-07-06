"""Tests for the aurora (OVATION) helpers — location-dynamic lookup, network mocked."""
import json
import os
import sys
import unittest
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


class TestGetAuroraGrid(unittest.TestCase):
    def test_builds_lonlat_lookup(self):
        payload = json.dumps({"coordinates": [[24, 57, 12], [19, 69, 40], [286, 40, 5]]}).encode()
        with patch("aurora.urllib.request.urlopen", return_value=_FakeResp(payload)):
            grid = aurora.get_aurora_grid()
        self.assertEqual(grid[(24, 57)], 12)
        self.assertEqual(grid[(19, 69)], 40)

    def test_empty_returns_none(self):
        with patch("aurora.urllib.request.urlopen", return_value=_FakeResp(json.dumps({"coordinates": []}).encode())):
            self.assertIsNone(aurora.get_aurora_grid())

    def test_error_returns_none(self):
        with patch("aurora.urllib.request.urlopen", side_effect=Exception("boom")):
            self.assertIsNone(aurora.get_aurora_grid())


class TestAuroraAt(unittest.TestCase):
    def setUp(self):
        self.grid = {(24, 57): 12, (286, 40): 5}

    def test_lookup_rounds(self):
        self.assertEqual(aurora.aurora_at(self.grid, 56.95, 24.10), 12)  # Riga

    def test_negative_longitude_wraps(self):
        # lon -74 -> 286 (New York-ish)
        self.assertEqual(aurora.aurora_at(self.grid, 40.4, -74.0), 5)

    def test_none_grid(self):
        self.assertIsNone(aurora.aurora_at(None, 57, 24))

    def test_missing_point(self):
        self.assertIsNone(aurora.aurora_at(self.grid, 0, 0))


class TestFormatAurora(unittest.TestCase):
    def test_number(self):
        self.assertEqual(aurora.format_aurora(23), "Aurora 23%")

    def test_zero_shows(self):
        self.assertEqual(aurora.format_aurora(0), "Aurora 0%")

    def test_none_empty(self):
        self.assertEqual(aurora.format_aurora(None), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
