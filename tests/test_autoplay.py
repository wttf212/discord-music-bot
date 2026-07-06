"""Tests for autoplay/endless-mode helpers (related-track discovery + pick)."""
import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_player
import commands
from track_queue import Track


class TestYoutubeVideoId(unittest.TestCase):
    def test_watch_url(self):
        self.assertEqual(audio_player._youtube_video_id("https://www.youtube.com/watch?v=abc123"), "abc123")

    def test_youtu_be(self):
        self.assertEqual(audio_player._youtube_video_id("https://youtu.be/xyz789"), "xyz789")

    def test_non_youtube(self):
        self.assertIsNone(audio_player._youtube_video_id("https://soundcloud.com/x/y"))

    def test_empty(self):
        self.assertIsNone(audio_player._youtube_video_id(""))


class TestGetRelatedTracks(unittest.TestCase):
    def test_filters_seed_and_returns_rest(self):
        seed = "https://www.youtube.com/watch?v=SEED"
        mix = {"title": "Mix", "tracks": [
            {"url": "https://www.youtube.com/watch?v=SEED", "title": "seed"},
            {"url": "https://www.youtube.com/watch?v=A", "title": "A"},
            {"url": "https://www.youtube.com/watch?v=B", "title": "B"},
        ]}
        with patch("audio_player.extract_playlist_info", return_value=mix) as m:
            out = audio_player.get_related_tracks(seed, "web", 25)
        self.assertEqual([t["url"] for t in out],
                         ["https://www.youtube.com/watch?v=A", "https://www.youtube.com/watch?v=B"])
        # Called with the RD mix URL for the seed id
        self.assertIn("list=RDSEED", m.call_args[0][0])

    def test_non_youtube_seed_returns_empty(self):
        with patch("audio_player.extract_playlist_info") as m:
            out = audio_player.get_related_tracks("https://soundcloud.com/x/y", "web")
        self.assertEqual(out, [])
        m.assert_not_called()

    def test_extract_failure_returns_empty(self):
        with patch("audio_player.extract_playlist_info", side_effect=Exception("boom")):
            out = audio_player.get_related_tracks("https://youtu.be/ID", "web")
        self.assertEqual(out, [])


class _GS:
    def __init__(self):
        self.autoplay = True
        self.autoplay_pool = []
        self.autoplay_history = set()


class _Bot:
    def __init__(self):
        self.config = {"youtube": {"client": "web"}}
        self._gs = _GS()

    def get_guild_state(self, gid):
        return self._gs


class TestAutoplayPick(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_pick_caches_pool_and_returns_track(self):
        bot = _Bot()
        seed = Track(query="q", title="Seed", requested_by="u1", url="https://www.youtube.com/watch?v=SEED")
        related = [
            {"url": "https://www.youtube.com/watch?v=A", "title": "A"},
            {"url": "https://www.youtube.com/watch?v=B", "title": "B"},
        ]
        with patch.object(commands, "get_related_tracks", return_value=related) as m:
            first = self._run(commands._autoplay_pick(bot, 1, seed))
            second = self._run(commands._autoplay_pick(bot, 1, seed))
        self.assertEqual(first.url, "https://www.youtube.com/watch?v=A")
        self.assertEqual(second.url, "https://www.youtube.com/watch?v=B")
        self.assertEqual(first.requested_by, "u1")  # attributed to seed requester
        self.assertEqual(m.call_count, 1)  # pool reused for the 2nd pick (anti-ban)
        self.assertIn("https://www.youtube.com/watch?v=A", bot._gs.autoplay_history)

    def test_pick_skips_history(self):
        bot = _Bot()
        bot._gs.autoplay_history.add("https://www.youtube.com/watch?v=A")
        seed = Track(query="q", title="Seed", requested_by="u1", url="https://www.youtube.com/watch?v=SEED")
        related = [
            {"url": "https://www.youtube.com/watch?v=A", "title": "A"},
            {"url": "https://www.youtube.com/watch?v=B", "title": "B"},
        ]
        with patch.object(commands, "get_related_tracks", return_value=related):
            pick = self._run(commands._autoplay_pick(bot, 1, seed))
        self.assertEqual(pick.url, "https://www.youtube.com/watch?v=B")  # A skipped (already played)

    def test_pick_empty_related_returns_none(self):
        bot = _Bot()
        seed = Track(query="q", title="Seed", requested_by="u1", url="https://www.youtube.com/watch?v=SEED")
        with patch.object(commands, "get_related_tracks", return_value=[]):
            self.assertIsNone(self._run(commands._autoplay_pick(bot, 1, seed)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
