"""Tests for EQ persistence helpers and preset resolver (Phase 07-01 — EQ-PERSIST-01, EQ-RANGE-01).

Tests exercise get_eq_bass, set_eq_bass, get_eq_treble, set_eq_treble,
EQ_PRESETS, and get_eq_preset_name WITHOUT touching the real guild_settings.json.
All file I/O is redirected to a temp directory per-test.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

# Ensure the project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import guild_settings
from guild_settings import (
    EQ_PRESETS,
    get_eq_bass,
    get_eq_preset_name,
    get_eq_treble,
    set_eq_bass,
    set_eq_treble,
)


class TestEqSettings(unittest.TestCase):
    """Per-test temp dir pointed at guild_settings.SETTINGS_FILE."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp_settings = os.path.join(self._tmpdir.name, "guild_settings.json")
        self._patcher = patch.object(guild_settings, "SETTINGS_FILE", self._tmp_settings)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    # ------------------------------------------------------------------
    # Default-value behaviour (EQ-PERSIST-01 — no prior set call needed)
    # ------------------------------------------------------------------

    def test_get_eq_bass_returns_0_for_unknown_guild(self):
        """Missing guild → bass default is 0, not None."""
        self.assertEqual(get_eq_bass("999"), 0)

    def test_get_eq_treble_returns_0_for_unknown_guild(self):
        """Missing guild → treble default is 0, not None."""
        self.assertEqual(get_eq_treble("999"), 0)

    def test_get_eq_bass_returns_0_when_key_missing_but_guild_exists(self):
        """Guild exists but eq_bass key absent → return 0."""
        with open(self._tmp_settings, "w") as fh:
            json.dump({"123": {"bitrate": 128}}, fh)
        self.assertEqual(get_eq_bass("123"), 0)

    def test_get_eq_treble_returns_0_when_key_missing_but_guild_exists(self):
        """Guild exists but eq_treble key absent → return 0."""
        with open(self._tmp_settings, "w") as fh:
            json.dump({"123": {"bitrate": 128}}, fh)
        self.assertEqual(get_eq_treble("123"), 0)

    # ------------------------------------------------------------------
    # Happy-path persistence (EQ-PERSIST-01)
    # ------------------------------------------------------------------

    def test_set_and_get_eq_bass_positive(self):
        set_eq_bass("123", 5)
        self.assertEqual(get_eq_bass("123"), 5)

    def test_set_and_get_eq_treble_negative(self):
        set_eq_treble("123", -10)
        self.assertEqual(get_eq_treble("123"), -10)

    def test_set_eq_bass_zero(self):
        set_eq_bass("123", 0)
        self.assertEqual(get_eq_bass("123"), 0)

    def test_set_eq_treble_max_boundary(self):
        set_eq_treble("123", 10)
        self.assertEqual(get_eq_treble("123"), 10)

    def test_set_eq_bass_min_boundary(self):
        set_eq_bass("123", -10)
        self.assertEqual(get_eq_bass("123"), -10)

    # ------------------------------------------------------------------
    # Non-interference with existing settings fields
    # ------------------------------------------------------------------

    def test_set_eq_bass_does_not_clobber_other_fields(self):
        """set_eq_bass must not remove bitrate, admins, or allowed_channel."""
        initial = {
            "123": {
                "bitrate": 256,
                "admins": ["42"],
                "allowed_channel": "9",
            }
        }
        with open(self._tmp_settings, "w") as fh:
            json.dump(initial, fh)

        set_eq_bass("123", 4)

        with open(self._tmp_settings, "r") as fh:
            data = json.load(fh)

        guild = data["123"]
        self.assertEqual(guild["bitrate"], 256)
        self.assertEqual(guild["admins"], ["42"])
        self.assertEqual(guild["allowed_channel"], "9")
        self.assertEqual(guild["eq_bass"], 4)

    def test_set_eq_treble_does_not_remove_eq_bass(self):
        """Setting treble must not remove an existing eq_bass value."""
        set_eq_bass("123", 3)
        set_eq_treble("123", 5)
        self.assertEqual(get_eq_bass("123"), 3)
        self.assertEqual(get_eq_treble("123"), 5)

    def test_set_eq_bass_does_not_remove_eq_treble(self):
        """Setting bass must not remove an existing eq_treble value."""
        set_eq_treble("123", 2)
        set_eq_bass("123", 7)
        self.assertEqual(get_eq_treble("123"), 2)
        self.assertEqual(get_eq_bass("123"), 7)

    # ------------------------------------------------------------------
    # Range validation — EQ-RANGE-01
    # ------------------------------------------------------------------

    def test_set_eq_bass_rejects_11(self):
        with self.assertRaises(ValueError) as ctx:
            set_eq_bass("123", 11)
        self.assertIn("-10", str(ctx.exception))
        self.assertIn("10", str(ctx.exception))

    def test_set_eq_bass_rejects_minus_11(self):
        with self.assertRaises(ValueError):
            set_eq_bass("123", -11)

    def test_set_eq_treble_rejects_11(self):
        with self.assertRaises(ValueError):
            set_eq_treble("123", 11)

    def test_set_eq_treble_rejects_minus_11(self):
        with self.assertRaises(ValueError):
            set_eq_treble("123", -11)

    def test_set_eq_bass_rejects_float(self):
        """D-03: integers only — 3.5 must raise ValueError."""
        with self.assertRaises(ValueError):
            set_eq_bass("123", 3.5)

    def test_set_eq_treble_rejects_float(self):
        with self.assertRaises(ValueError):
            set_eq_treble("123", -1.5)

    def test_set_eq_bass_rejects_bool_true(self):
        """bool is a subclass of int in Python — must be explicitly rejected."""
        with self.assertRaises(ValueError):
            set_eq_bass("123", True)

    def test_set_eq_bass_rejects_bool_false(self):
        with self.assertRaises(ValueError):
            set_eq_bass("123", False)

    def test_set_eq_bass_rejects_string(self):
        with self.assertRaises((ValueError, TypeError)):
            set_eq_bass("123", "5")

    def test_out_of_range_does_not_write_to_disk(self):
        """Invalid value must be rejected BEFORE save_settings is called."""
        with self.assertRaises(ValueError):
            set_eq_bass("123", 99)
        # File should not exist (or remain empty dict if it did)
        if os.path.isfile(self._tmp_settings):
            with open(self._tmp_settings, "r") as fh:
                data = json.load(fh)
            self.assertNotIn("123", data)

    # ------------------------------------------------------------------
    # EQ_PRESETS constant (D-05)
    # ------------------------------------------------------------------

    def test_eq_presets_has_four_entries(self):
        self.assertEqual(len(EQ_PRESETS), 4)

    def test_eq_presets_flat(self):
        self.assertEqual(EQ_PRESETS["flat"], (0, 0))

    def test_eq_presets_bass_boost(self):
        self.assertEqual(EQ_PRESETS["bass-boost"], (5, 0))

    def test_eq_presets_treble_boost(self):
        self.assertEqual(EQ_PRESETS["treble-boost"], (0, 5))

    def test_eq_presets_vocal(self):
        self.assertEqual(EQ_PRESETS["vocal"], (-2, 3))

    # ------------------------------------------------------------------
    # get_eq_preset_name resolver
    # ------------------------------------------------------------------

    def test_preset_name_flat(self):
        self.assertEqual(get_eq_preset_name(0, 0), "flat")

    def test_preset_name_bass_boost(self):
        self.assertEqual(get_eq_preset_name(5, 0), "bass-boost")

    def test_preset_name_treble_boost(self):
        self.assertEqual(get_eq_preset_name(0, 5), "treble-boost")

    def test_preset_name_vocal(self):
        self.assertEqual(get_eq_preset_name(-2, 3), "vocal")

    def test_preset_name_custom_when_no_match(self):
        self.assertEqual(get_eq_preset_name(3, 1), "custom")

    def test_preset_name_custom_for_arbitrary_values(self):
        self.assertEqual(get_eq_preset_name(7, 2), "custom")

    def test_preset_name_custom_for_negative_values(self):
        self.assertEqual(get_eq_preset_name(-5, -5), "custom")


if __name__ == "__main__":
    unittest.main(verbosity=2)
