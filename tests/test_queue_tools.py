"""Tests for loop modes and queue-management tools on TrackQueue."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from track_queue import TrackQueue, Track


def T(name, user="u1"):
    return Track(query=name, title=name, requested_by=user, url=f"https://x/{name}")


class TestLoopModes(unittest.TestCase):
    def setUp(self):
        self.q = TrackQueue()
        self.q.fair_play = False  # deterministic ordering for these tests

    def test_cycle_loop(self):
        self.assertEqual(self.q.loop_mode, "off")
        self.assertEqual(self.q.cycle_loop(), "track")
        self.assertEqual(self.q.cycle_loop(), "queue")
        self.assertEqual(self.q.cycle_loop(), "off")

    def test_track_loop_replays_current(self):
        self.q.add(T("a")); self.q.add(T("b"))
        first = self.q.next()            # plays a
        self.assertEqual(first.title, "a")
        self.q.loop_mode = "track"
        again = self.q.next()            # natural end → replay a
        self.assertIs(again, first)
        self.assertEqual([t.title for t in self.q.list()], ["b"])  # b untouched

    def test_track_loop_force_skips(self):
        self.q.add(T("a")); self.q.add(T("b"))
        self.q.next()                    # a
        self.q.loop_mode = "track"
        nxt = self.q.next(force=True)     # manual skip → advance to b
        self.assertEqual(nxt.title, "b")

    def test_queue_loop_cycles(self):
        self.q.add(T("a")); self.q.add(T("b"))
        self.q.loop_mode = "queue"
        self.assertEqual(self.q.next().title, "a")  # play a, nothing cycled yet
        self.assertEqual(self.q.next().title, "b")  # a cycles to back
        self.assertEqual(self.q.next().title, "a")  # b cycles to back, a returns

    def test_queue_loop_single_track(self):
        self.q.add(T("a"))
        self.q.loop_mode = "queue"
        self.assertEqual(self.q.next().title, "a")
        self.assertEqual(self.q.next().title, "a")  # replays the lone track

    def test_clear_resets_loop(self):
        self.q.loop_mode = "queue"
        self.q.clear()
        self.assertEqual(self.q.loop_mode, "off")


class TestQueueTools(unittest.TestCase):
    def setUp(self):
        self.q = TrackQueue()
        self.q.fair_play = False
        for n in ("a", "b", "c", "d"):
            self.q.add(T(n))

    def test_remove_valid(self):
        removed = self.q.remove(2)
        self.assertEqual(removed.title, "b")
        self.assertEqual([t.title for t in self.q.list()], ["a", "c", "d"])

    def test_remove_out_of_range(self):
        self.assertIsNone(self.q.remove(0))
        self.assertIsNone(self.q.remove(99))
        self.assertEqual(len(self.q.list()), 4)

    def test_move(self):
        moved = self.q.move(1, 3)
        self.assertEqual(moved.title, "a")
        self.assertEqual([t.title for t in self.q.list()], ["b", "c", "a", "d"])

    def test_move_out_of_range(self):
        self.assertIsNone(self.q.move(1, 99))
        self.assertEqual([t.title for t in self.q.list()], ["a", "b", "c", "d"])

    def test_skip_to(self):
        ok = self.q.skip_to(3)  # drop a, b; c is next
        self.assertTrue(ok)
        self.assertEqual([t.title for t in self.q.list()], ["c", "d"])

    def test_skip_to_out_of_range(self):
        self.assertFalse(self.q.skip_to(99))
        self.assertEqual(len(self.q.list()), 4)

    def test_clear_upcoming(self):
        n = self.q.clear_upcoming()
        self.assertEqual(n, 4)
        self.assertEqual(self.q.list(), [])

    def test_dedupe(self):
        self.q.add(T("b"))  # duplicate of existing b (same url)
        self.q.add(T("a"))  # duplicate of a
        removed = self.q.dedupe()
        self.assertEqual(removed, 2)
        self.assertEqual([t.title for t in self.q.list()], ["a", "b", "c", "d"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
