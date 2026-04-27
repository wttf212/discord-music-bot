"""Tests for Track.is_radio field and TrackQueue.next() history guard (D-11).

RED phase: these tests fail before the implementation is added.
"""

import unittest
from track_queue import Track, TrackQueue


class TestTrackIsRadioField(unittest.TestCase):
    """Track dataclass must have an is_radio: bool = False field."""

    def test_is_radio_defaults_to_false(self):
        t = Track(query="q", title="t", requested_by="u")
        self.assertIs(t.is_radio, False)

    def test_is_radio_true_when_set(self):
        t = Track(query="q", title="t", requested_by="u", is_radio=True)
        self.assertIs(t.is_radio, True)

    def test_existing_construction_still_works(self):
        """Constructing Track with all positional+keyword args must not break."""
        t = Track(query="q", title="t", requested_by="u", thumbnail="x", url="y")
        self.assertIs(t.is_radio, False)

    def test_is_radio_false_explicit(self):
        t = Track(query="q", title="t", requested_by="u", is_radio=False)
        self.assertIs(t.is_radio, False)


class TestTrackQueueHistoryGuard(unittest.TestCase):
    """TrackQueue.next() must NOT append radio tracks to _history (D-11)."""

    def test_radio_track_not_added_to_history(self):
        q = TrackQueue()
        radio = Track(query="BBC", title="BBC Radio 1", requested_by="u1", is_radio=True)
        next_track = Track(query="s", title="Song", requested_by="u2")
        q.current = radio
        q.add(next_track)
        q.next()
        self.assertEqual(len(q._history), 0,
                         "Radio track must NOT be appended to _history")

    def test_normal_track_still_added_to_history(self):
        q = TrackQueue()
        normal = Track(query="s", title="Song", requested_by="u1")
        next_track = Track(query="s2", title="Song 2", requested_by="u2")
        q.current = normal
        q.add(next_track)
        q.next()
        self.assertEqual(len(q._history), 1,
                         "Normal track MUST be appended to _history")
        self.assertEqual(q._history[0], normal)

    def test_previous_not_modified(self):
        """TrackQueue.previous() must still work normally."""
        q = TrackQueue()
        t1 = Track(query="s1", title="Song 1", requested_by="u")
        t2 = Track(query="s2", title="Song 2", requested_by="u")
        q.add(t1)
        q.add(t2)
        q.next()
        q.next()
        prev = q.previous()
        self.assertIsNotNone(prev)


if __name__ == "__main__":
    unittest.main()
