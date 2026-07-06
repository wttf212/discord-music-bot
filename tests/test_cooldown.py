"""Tests for the per-user button cooldown that throttles card-button spam."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import commands


class TestButtonCooldown(unittest.TestCase):
    def setUp(self):
        commands._button_cooldowns.clear()

    def test_first_allowed_repeat_blocked(self):
        self.assertFalse(commands._on_cooldown(1, 2, "skip", 2.0))  # first use
        self.assertTrue(commands._on_cooldown(1, 2, "skip", 2.0))   # rapid repeat blocked

    def test_independent_per_user_and_action(self):
        self.assertFalse(commands._on_cooldown(1, 2, "skip", 2.0))
        self.assertFalse(commands._on_cooldown(1, 2, "loop", 2.0))   # different action
        self.assertFalse(commands._on_cooldown(1, 3, "skip", 2.0))   # different user
        self.assertFalse(commands._on_cooldown(9, 2, "skip", 2.0))   # different guild

    def test_expires_after_window(self):
        with patch("commands.time.monotonic", side_effect=[100.0, 100.5, 103.0]):
            self.assertFalse(commands._on_cooldown(1, 2, "skip", 2.0))  # t=100 → set
            self.assertTrue(commands._on_cooldown(1, 2, "skip", 2.0))   # t=100.5 → blocked
            self.assertFalse(commands._on_cooldown(1, 2, "skip", 2.0))  # t=103 → window passed


if __name__ == "__main__":
    unittest.main(verbosity=2)
