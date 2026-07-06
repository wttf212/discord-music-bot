"""Test the grab embed builder (used by !grab and the card grab button)."""
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import commands
from track_queue import Track


class TestGrabEmbed(unittest.TestCase):
    def _gs(self, title="Now Song", artist="The Artist"):
        gs = MagicMock()
        gs.player.current_track_title = title
        gs.player.current_artist = artist
        return gs

    def test_embed_has_title_artist_url_thumb(self):
        gs = self._gs()
        track = Track(query="q", title="q", requested_by="u1",
                      url="https://youtu.be/abc", thumbnail="https://t/x.jpg")
        embed = commands._build_grab_embed(gs, track)
        self.assertEqual(embed.title, "Now Song")
        self.assertIn("by The Artist", embed.description)
        self.assertIn("https://youtu.be/abc", embed.description)
        self.assertEqual(embed.thumbnail.url, "https://t/x.jpg")

    def test_falls_back_to_track_title(self):
        gs = self._gs(title=None, artist="")
        track = Track(query="q", title="Track Title", requested_by="u1")
        embed = commands._build_grab_embed(gs, track)
        self.assertEqual(embed.title, "Track Title")


if __name__ == "__main__":
    unittest.main(verbosity=2)
