"""Tests for Spotify link parsing and resolution (network mocked)."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import spotify
from spotify import is_spotify_url, resolve_spotify, SpotifyError, _parse


class TestIsSpotifyUrl(unittest.TestCase):
    def test_open_url(self):
        self.assertTrue(is_spotify_url("https://open.spotify.com/track/abc"))

    def test_uri(self):
        self.assertTrue(is_spotify_url("spotify:track:abc"))

    def test_non_spotify(self):
        self.assertFalse(is_spotify_url("https://youtube.com/watch?v=x"))

    def test_empty(self):
        self.assertFalse(is_spotify_url(""))


class TestParse(unittest.TestCase):
    def test_track(self):
        self.assertEqual(_parse("https://open.spotify.com/track/ID123"), ("track", "ID123"))

    def test_playlist_with_query(self):
        self.assertEqual(_parse("https://open.spotify.com/playlist/PID?si=xyz"), ("playlist", "PID"))

    def test_album(self):
        self.assertEqual(_parse("https://open.spotify.com/album/AID"), ("album", "AID"))

    def test_intl_path(self):
        self.assertEqual(_parse("https://open.spotify.com/intl-de/track/ID"), ("track", "ID"))

    def test_uri(self):
        self.assertEqual(_parse("spotify:playlist:PID"), ("playlist", "PID"))

    def test_unknown(self):
        self.assertEqual(_parse("https://open.spotify.com/artist/AID"), (None, None))


class TestResolveTrack(unittest.TestCase):
    def test_track_with_creds(self):
        with patch("spotify._get_token", return_value="tok"), \
             patch("spotify._api", return_value={"name": "Song", "artists": [{"name": "Artist"}]}):
            r = resolve_spotify("https://open.spotify.com/track/ID", "cid", "csec")
        self.assertEqual(r["kind"], "track")
        self.assertEqual(r["tracks"], [{"query": "Artist - Song", "title": "Artist - Song"}])

    def test_track_without_creds_uses_oembed(self):
        with patch("spotify._oembed_title", return_value="Some Song"):
            r = resolve_spotify("https://open.spotify.com/track/ID")
        self.assertEqual(r["tracks"][0]["query"], "Some Song")

    def test_track_oembed_failure(self):
        with patch("spotify._oembed_title", return_value=None):
            with self.assertRaises(SpotifyError):
                resolve_spotify("https://open.spotify.com/track/ID")


class TestResolveCollections(unittest.TestCase):
    def test_playlist_requires_creds(self):
        with self.assertRaises(SpotifyError):
            resolve_spotify("https://open.spotify.com/playlist/PID")

    def test_playlist_with_creds(self):
        def fake_api(url, tok):
            if "playlists" in url and "fields=name" in url:
                return {"name": "My Playlist"}
            if "/tracks?" in url:
                return {"items": [
                    {"track": {"name": "S1", "artists": [{"name": "A1"}]}},
                    {"track": {"name": "S2", "artists": [{"name": "A2"}]}},
                    {"track": None},  # unavailable track — skipped
                ], "next": None}
            return {}
        with patch("spotify._get_token", return_value="tok"), patch("spotify._api", side_effect=fake_api):
            r = resolve_spotify("https://open.spotify.com/playlist/PID", "cid", "csec")
        self.assertEqual(r["title"], "My Playlist")
        self.assertEqual([t["query"] for t in r["tracks"]], ["A1 - S1", "A2 - S2"])

    def test_album_with_creds(self):
        def fake_api(url, tok):
            if "/albums/" in url:
                return {"name": "Alb", "tracks": {"items": [
                    {"name": "T1", "artists": [{"name": "A"}]},
                    {"name": "T2", "artists": [{"name": "A"}]},
                ], "next": None}}
            return {}
        with patch("spotify._get_token", return_value="tok"), patch("spotify._api", side_effect=fake_api):
            r = resolve_spotify("https://open.spotify.com/album/AID", "cid", "csec")
        self.assertEqual(r["title"], "Alb")
        self.assertEqual([t["query"] for t in r["tracks"]], ["A - T1", "A - T2"])

    def test_unrecognised_link(self):
        with self.assertRaises(SpotifyError):
            resolve_spotify("https://open.spotify.com/artist/AID", "cid", "csec")


if __name__ == "__main__":
    unittest.main(verbosity=2)
