"""Tests for _build_ffmpeg_af_options (Phase 07-02)."""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from audio_player import _build_ffmpeg_af_options


class TestBuildAf(unittest.TestCase):
    def test_flat_returns_empty(self):
        self.assertEqual(_build_ffmpeg_af_options(0, 0), "")

    LIMITER = "alimiter=level_out=0.9:attack=5:release=50"

    def test_bass_only(self):
        self.assertEqual(_build_ffmpeg_af_options(5, 0), f"bass=g=5,{self.LIMITER}")

    def test_treble_only_negative(self):
        self.assertEqual(_build_ffmpeg_af_options(0, -3), f"treble=g=-3,{self.LIMITER}")

    def test_both(self):
        self.assertEqual(_build_ffmpeg_af_options(5, -2), f"bass=g=5,treble=g=-2,{self.LIMITER}")

    def test_boundaries(self):
        self.assertEqual(_build_ffmpeg_af_options(-10, 10), f"bass=g=-10,treble=g=10,{self.LIMITER}")


if __name__ == "__main__":
    unittest.main()
