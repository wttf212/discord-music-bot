"""Tests for extract_playlist_info's limit parameter (playlist early-start).

limit=1 lets play() fetch just the first track fast (one page) so audio starts
before the full enumeration finishes. Network is mocked.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_player
from audio_player import extract_playlist_info, MAX_PLAYLIST_TRACKS


def _mock_ydl(entries, title="My Playlist"):
    cm = MagicMock()
    cm.extract_info.return_value = {"title": title, "entries": entries}
    ydl_class = MagicMock()
    ydl_class.return_value.__enter__.return_value = cm
    ydl_class.return_value.__exit__.return_value = False
    return ydl_class


class TestExtractPlaylistLimit(unittest.TestCase):
    def _entries(self, n):
        return [{"url": f"id{i}", "title": f"T{i}"} for i in range(1, n + 1)]

    def test_limit_1_returns_single_track(self):
        ydl_class = _mock_ydl(self._entries(5))
        with patch("audio_player.YoutubeDL", ydl_class):
            out = extract_playlist_info("https://www.youtube.com/playlist?list=X", "web", limit=1)
        self.assertEqual(len(out["tracks"]), 1)
        self.assertEqual(out["tracks"][0]["title"], "T1")
        self.assertEqual(out["title"], "My Playlist")

    def test_limit_1_sets_playlistend_1(self):
        ydl_class = _mock_ydl(self._entries(5))
        with patch("audio_player.YoutubeDL", ydl_class):
            extract_playlist_info("https://www.youtube.com/playlist?list=X", "web", limit=1)
        opts = ydl_class.call_args[0][0]
        self.assertEqual(opts["playlistend"], 1)

    def test_default_limit_is_max(self):
        ydl_class = _mock_ydl(self._entries(3))
        with patch("audio_player.YoutubeDL", ydl_class):
            extract_playlist_info("https://www.youtube.com/playlist?list=X", "web")
        opts = ydl_class.call_args[0][0]
        self.assertEqual(opts["playlistend"], MAX_PLAYLIST_TRACKS)

    def test_limit_clamped_low(self):
        ydl_class = _mock_ydl(self._entries(3))
        with patch("audio_player.YoutubeDL", ydl_class):
            extract_playlist_info("https://www.youtube.com/playlist?list=X", "web", limit=0)
        opts = ydl_class.call_args[0][0]
        self.assertEqual(opts["playlistend"], 1)

    def test_limit_clamped_high(self):
        ydl_class = _mock_ydl(self._entries(3))
        with patch("audio_player.YoutubeDL", ydl_class):
            extract_playlist_info("https://www.youtube.com/playlist?list=X", "web", limit=99999)
        opts = ydl_class.call_args[0][0]
        self.assertEqual(opts["playlistend"], MAX_PLAYLIST_TRACKS)

    def test_youtube_flat_id_becomes_watch_url(self):
        ydl_class = _mock_ydl(self._entries(1))
        with patch("audio_player.YoutubeDL", ydl_class):
            out = extract_playlist_info("https://www.youtube.com/playlist?list=X", "web", limit=1)
        self.assertEqual(out["tracks"][0]["url"], "https://www.youtube.com/watch?v=id1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
