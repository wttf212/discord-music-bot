"""Tests for TrackQueue hardening: bounded history, O(1)-copy-free length/iteration,
and the single-source-of-truth MAX_PLAYLIST_TRACKS shared with spotify.py."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from track_queue import TrackQueue, Track


def T(name, user="u1"):
    return Track(query=name, title=name, requested_by=user, url=f"https://x/{name}")


class TestQueueLenIter(unittest.TestCase):
    def setUp(self):
        self.q = TrackQueue()
        self.q.fair_play = False  # deterministic ordering for these tests

    def test_len_reflects_pending_count_only(self):
        self.q.add(T("a")); self.q.add(T("b")); self.q.add(T("c"))
        self.assertEqual(len(self.q), 3)
        self.q.next()  # "a" becomes current, moves out of pending
        self.assertEqual(len(self.q), 2)

    def test_len_unaffected_by_history(self):
        self.q.add(T("a")); self.q.add(T("b"))
        self.q.next()  # current=a
        self.q.next()  # current=b, a -> history
        self.assertEqual(len(self.q), 0)
        self.assertEqual(len(self.q._history), 1)

    def test_iter_yields_pending_in_order(self):
        self.q.add(T("a")); self.q.add(T("b")); self.q.add(T("c"))
        self.assertEqual([t.title for t in self.q], ["a", "b", "c"])

    def test_iter_matches_list(self):
        self.q.add(T("a")); self.q.add(T("b"))
        self.assertEqual(list(iter(self.q)), self.q.list())


class TestHistoryBounded(unittest.TestCase):
    def setUp(self):
        self.q = TrackQueue()
        self.q.fair_play = False

    def test_history_bounded_to_100(self):
        # Play 150 tracks sequentially so each finished one goes to history.
        for i in range(151):
            self.q.add(T(f"t{i}"))
        for _ in range(150):
            self.q.next()
        self.assertLessEqual(len(self.q._history), 100)
        self.assertEqual(len(self.q._history), 100)

    def test_previous_still_returns_most_recent_after_overflow(self):
        for i in range(151):
            self.q.add(T(f"t{i}"))
        for _ in range(150):
            self.q.next()
        # current is t149; the most-recently-played-before-current is t148 (the
        # newest entry in the bounded history), even though history dropped the
        # oldest entries (t0..t48) to stay at maxlen=100.
        prev = self.q.previous()
        self.assertEqual(prev.title, "t148")


class TestSpotifySharedConstant(unittest.TestCase):
    def test_spotify_imports_max_playlist_tracks(self):
        import spotify
        self.assertEqual(spotify.MAX_PLAYLIST_TRACKS, 2000)

    def test_spotify_has_no_own_max_tracks(self):
        import spotify
        self.assertFalse(hasattr(spotify, "MAX_TRACKS"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
