"""Tests for the fair_play x loop_mode='queue' intersection on TrackQueue.

Pins the lap rotation (every queued track plays exactly once per lap) and the
preview/playback parity the Up Next card and the CDN prefetch depend on.
"""
import copy
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from track_queue import TrackQueue, Track


def T(name, user="u1"):
    return Track(query=name, title=name, requested_by=user, url=f"https://x/{name}")


def roster(*groups):
    """(prefix, user, count) triples -> [(title, user), ...] in queue order."""
    out = []
    for prefix, user, count in groups:
        out.extend((f"{prefix}{i}", user) for i in range(1, count + 1))
    return out


def build(spec, loop="off", fair_play=True):
    q = TrackQueue()
    q.loop_mode = loop
    q.fair_play = fair_play
    for title, user in spec:
        q.add(T(title, user))
    return q


SHAPES = {
    "6A+1B": roster(("A", "u1", 6), ("B", "u2", 1)),
    "3A+3B": roster(("A", "u1", 3), ("B", "u2", 3)),
    "5A+1B+1C": roster(("A", "u1", 5), ("B", "u2", 1), ("C", "u3", 1)),
    "2A+1B": roster(("A", "u1", 2), ("B", "u2", 1)),
    "1A+1B": roster(("A", "u1", 1), ("B", "u2", 1)),
    "4A": roster(("A", "u1", 4)),
}


class TestLapRotation(unittest.TestCase):
    def test_minority_user_plays_once_per_lap(self):
        # THE REPRO. Before the lap fix, 6 tracks by u1 + 1 by u2 under
        # loop='queue' produced (observed on c241fb2):
        #   A1,B1,A2,B1,A3,B1,A4,B1,A5,B1,A6,B1,A1,B1,A2,B1,A3,B1,A4,B1,A5
        # B1 took 10 of the 21 slots because the finished track is re-appended to
        # the very deque the greedy one-step fair-play scan then reads.
        q = build(SHAPES["6A+1B"], loop="queue")
        picks = [q.next().title for _ in range(21)]
        all_titles = ["A1", "A2", "A3", "A4", "A5", "A6", "B1"]

        # Lap 1 is identical to what fair_play=True + loop='off' produces today,
        # so the first-pass interleaving is preserved.
        self.assertEqual(picks[0:7], ["A1", "B1", "A2", "A3", "A4", "A5", "A6"])
        # Laps 2+ start with the minority user (B1 is the only track by u2 that
        # has not played yet when the lap rolls). Lap 1 != lap 2 by design.
        self.assertEqual(picks[7:14], ["B1", "A1", "A2", "A3", "A4", "A5", "A6"])
        self.assertEqual(picks[14:21], picks[7:14])
        # Each LAP (not each sliding window -- lap 1 ends A6 and lap 2 opens B1)
        # is a permutation of the whole queue.
        for start in (0, 7, 14):
            self.assertEqual(sorted(picks[start:start + 7]), sorted(all_titles))
        self.assertEqual(picks.count("B1"), 3)

    def test_lap_order_is_stable(self):
        q = build(SHAPES["6A+1B"], loop="queue")
        picks = [q.next().title for _ in range(28)]
        laps = [picks[i:i + 7] for i in range(0, 28, 7)]
        self.assertEqual(laps[1], laps[2])
        self.assertEqual(laps[2], laps[3])

    def test_lap_contains_every_track_exactly_once(self):
        spec = roster(("A", "u1", 3), ("B", "u2", 2), ("C", "u3", 1))
        q = build(spec, loop="queue")
        picks = [q.next().title for _ in range(18)]
        expected = sorted(title for title, _ in spec)
        for start in (0, 6, 12):
            self.assertEqual(sorted(picks[start:start + 6]), expected)

    def test_remove_last_unplayed_rolls_the_lap(self):
        q = build(roster(("A", "u1", 3)), loop="queue")
        q.next()
        q.next()
        # _queue is [A3, A1]: A3 is the only pending track still unplayed.
        self.assertEqual([t.title for t in q.list()], ["A3", "A1"])
        self.assertEqual(q.remove(1).title, "A3")

        pv = q.preview_fair_order(1)
        nxt = q.next()
        self.assertIsNotNone(nxt)
        self.assertTrue(pv and pv[0] is nxt)
        rest = [nxt.title] + [q.next().title for _ in range(5)]
        self.assertEqual(rest, ["A1", "A2", "A1", "A2", "A1", "A2"])

    def test_leaving_queue_loop_clears_lap_marks(self):
        q = build(roster(("A", "u1", 4)), loop="queue")
        q.next()
        q.next()
        q.loop_mode = "off"  # mimics commands.py:2953 -- direct assignment, not cycle_loop()
        order = [t.title for t in q.list()]
        self.assertEqual(order, ["A3", "A4", "A1"])
        self.assertEqual([q.next().title for _ in order], order)

        # queue -> off -> queue round trip: nothing lost, nothing duplicated,
        # and no stale mark survives the trip.
        q2 = build(roster(("A", "u1", 4)), loop="queue")
        q2.next()
        q2.next()          # mid-lap under the queue loop: A1 is marked
        q2.loop_mode = "off"
        q2.next()          # A3 plays, A2 files into history
        q2.loop_mode = "queue"
        q2.next()          # A4
        q2.next()          # the lap rolls over and A1 plays again
        seen = ([t.title for t in q2.list()] + [q2.current.title]
                + [t.title for t in q2._history])
        self.assertEqual(sorted(seen), ["A1", "A2", "A3", "A4"])
        self.assertTrue(all(t.lap_played is False for t in q2.list()))

    def test_radio_current_is_not_recycled_or_marked(self):
        q = build(roster(("A", "u1", 2)), loop="queue")
        radio = Track(query="stream", title="Radio", requested_by="u2", is_radio=True)
        q.current = radio
        q.last_played_user = "u2"

        self.assertEqual(q.next().title, "A1")
        self.assertFalse(any(t is radio for t in q.list()))
        self.assertFalse(any(t is radio for t in q._history))
        self.assertIs(radio.lap_played, False)


class TestPreviewParity(unittest.TestCase):
    def test_preview_matches_next_at_every_step(self):
        for name, spec in SHAPES.items():
            for fair in (True, False):
                for loop in ("off", "track", "queue"):
                    # loop "track" with force=False returns self.current WITHOUT popping, so preview
                    # (which only ever names a PENDING track) can never match it. force=True is the
                    # manual-skip path and is the only one where track-loop advances the queue.
                    # Do NOT "fix" this by making preview return [current] * limit -- the prefetch at
                    # commands.py:1894 writes resolved_info onto preview[0] and commands.py:1918 calls
                    # discard() on it by identity; current is not in _queue, so that would abort the
                    # dead-track prefetch loop.
                    force = loop == "track"
                    for depth in range(14):
                        with self.subTest(shape=name, fair_play=fair, loop=loop, depth=depth):
                            q = build(spec, loop=loop, fair_play=fair)
                            for _ in range(depth):
                                if not len(q):
                                    break
                                q.next(force=force)
                            for _ in range(20):
                                if not len(q):
                                    break
                                pv = q.preview_fair_order(1)
                                nxt = q.next(force=force)
                                self.assertTrue(pv and pv[0] is nxt)

    def test_preview_multi_step_matches_consecutive_next_calls(self):
        for name, spec in SHAPES.items():
            for loop in ("off", "queue"):
                for limit in (5, 10):
                    with self.subTest(shape=name, loop=loop, limit=limit):
                        q = build(spec, loop=loop)
                        q.next()  # warm up so last_played_user is set
                        before = copy.deepcopy(q.list())
                        n_before, current_before = len(q), q.current

                        pv = q.preview_fair_order(limit)

                        self.assertEqual(q.list(), before)  # preview mutated nothing
                        self.assertEqual(len(q), n_before)
                        self.assertIs(q.current, current_before)
                        self.assertTrue(pv)
                        for expected in pv:
                            self.assertIs(q.next(), expected)

    def test_single_track_queue_loop_preview_is_empty(self):
        q = build(roster(("A", "u1", 1)), loop="queue")
        first = q.next()
        self.assertEqual(len(q), 0)
        self.assertIs(q.next(), first)  # early return: the lone track replays
        # Nothing is PENDING, so preview names nothing. This pins the `blocked`
        # index and preserves the prefetch's "nothing queued" bail.
        self.assertEqual(q.preview_fair_order(5), [])

    def test_preview_first_entry_is_always_a_pending_track(self):
        # The discard() contract at commands.py:1918: the prefetch target must be
        # an object that is actually in _queue, or the identity removal no-ops.
        rng = random.Random(0)
        for _ in range(200):
            n = rng.randint(1, 6)
            spec = [(f"T{i}", f"u{rng.randint(1, 3)}") for i in range(1, n + 1)]
            q = build(spec, loop=rng.choice(("off", "track", "queue")),
                      fair_play=rng.choice((True, False)))
            for _ in range(rng.randint(0, 12)):
                if not len(q):
                    break
                q.next(force=rng.choice((True, False)))
                pv = q.preview_fair_order(1)
                if pv:
                    self.assertTrue(any(t is pv[0] for t in q.list()))


class TestRegressionPins(unittest.TestCase):
    def test_loop_off_ordering_unchanged(self):
        # The fair-play front-pull had no test at all before this file: the whole
        # block could be deleted with all 517 tests still green. These two literal
        # sequences close that hole.
        cases = [
            (roster(("A", "u1", 6), ("B", "u2", 1)),
             ["A1", "B1", "A2", "A3", "A4", "A5", "A6"]),
            (roster(("A", "u1", 3), ("B", "u2", 2), ("C", "u3", 1)),
             ["A1", "B1", "A2", "B2", "A3", "C1"]),
        ]
        for spec, expected in cases:
            with self.subTest(size=len(spec)):
                q = build(spec, loop="off")
                self.assertEqual([q.next().title for _ in expected], expected)

    def test_fair_play_off_loop_queue_is_plain_fifo(self):
        q = build(roster(("A", "u1", 3), ("B", "u2", 1)), loop="queue", fair_play=False)
        expected = ["A1", "A2", "A3", "B1"] * 2
        self.assertEqual([q.next().title for _ in expected], expected)

    def test_next_removes_by_identity_not_equality(self):
        twin_a, twin_b = T("A1", "u1"), T("A1", "u1")
        self.assertEqual(twin_a, twin_b)  # value-equal dataclasses...
        self.assertIsNot(twin_a, twin_b)  # ...but distinct objects
        q = TrackQueue()
        q.loop_mode = "queue"
        for t in (twin_a, twin_b, T("B1", "u2")):
            q.add(t)

        first = q.next()
        self.assertIs(first, twin_a)
        self.assertIs(q.list()[0], twin_b)  # the twin survived, not removed by ==
        self.assertFalse(any(t is first for t in q.list()))
        for _ in range(5):
            nxt = q.next()
            self.assertFalse(any(t is nxt for t in q.list()))
            self.assertEqual(len(q), 2)

    def test_clear_leaks_no_lap_state(self):
        spec = roster(("A", "u1", 6), ("B", "u2", 1))
        q = build(spec, loop="queue")
        q.fair_play = False
        q.next()
        q.next()
        q.next()
        q.clear()
        self.assertEqual(q.loop_mode, "off")
        self.assertTrue(q.fair_play)  # clear() restores the default; only loop_mode was pinned before

        for title, user in spec:
            q.add(T(title, user))
        q.loop_mode = "queue"
        picks = [q.next().title for _ in range(7)]
        self.assertEqual(picks, ["A1", "B1", "A2", "A3", "A4", "A5", "A6"])


class TestMutationsMidLap(unittest.TestCase):
    def test_new_track_mid_lap_is_heard_next(self):
        # The guard that rejected the frozen-window design: a track added mid-lap
        # joins the CURRENT lap, it does not wait for the next one.
        q = build(roster(("A", "u1", 4)), loop="queue")
        picks = [q.next().title, q.next().title]
        q.add(T("B1", "u2"))
        self.assertEqual(q.next().title, "B1")
        self.assertEqual(picks, ["A1", "A2"])  # no already-played track jumped in first

    def test_requeue_front_loop_queue_multi_user(self):
        q = build(roster(("A", "u1", 2), ("B", "u2", 1)), loop="queue")
        played = q.next()
        q.requeue_front(played)
        self.assertIsNone(q.current)
        self.assertEqual(len(q), 3)

        # Fair play still interleaves: last_played_user is the requeued track's
        # user, so another user's track opens. What matters is that the requeued
        # track is neither lost nor replayed -- it takes exactly one slot per lap.
        picks = [q.next() for _ in range(6)]
        titles = [t.title for t in picks]
        self.assertEqual(sorted(titles[0:3]), ["A1", "A2", "B1"])
        self.assertEqual(sorted(titles[3:6]), ["A1", "A2", "B1"])
        self.assertTrue(any(t is played for t in picks[0:3]))

    def test_move_makes_a_played_track_play_next(self):
        q = build(roster(("A", "u1", 4)), loop="queue")
        q.next()
        q.next()  # A1 has already played this lap
        self.assertEqual([t.title for t in q.list()], ["A3", "A4", "A1"])
        moved = q.move(3, 1)
        self.assertIs(moved, q.list()[0])
        self.assertEqual(q.next().title, "A1")

    def test_shuffle_starts_a_fresh_lap(self):
        q = build(roster(("A", "u1", 4)), loop="queue")
        q.next()
        q.next()  # mid-lap: A1 has played
        pending = sorted(t.title for t in q.list())
        self.assertEqual(q.shuffle(), 3)
        picks = [q.next().title for _ in pending]
        self.assertEqual(sorted(picks), pending)  # a full fresh lap, no repeats

    def test_skip_to_mid_lap_does_not_wedge(self):
        q = build(roster(("A", "u1", 4)), loop="queue")
        q.next()
        q.next()
        self.assertTrue(q.skip_to(3))  # drops A3, A4 -- only the already-played A1 is left
        self.assertEqual([t.title for t in q.list()], ["A1"])
        picks = [q.next() for _ in range(6)]
        self.assertTrue(all(p is not None for p in picks))
        self.assertEqual({p.title for p in picks}, {"A1", "A2"})
        self.assertEqual(len(q), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
