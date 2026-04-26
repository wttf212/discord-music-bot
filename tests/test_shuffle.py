"""Tests for TrackQueue.shuffle() method."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from track_queue import TrackQueue, Track


def _make_track(title: str, user: str = "u") -> Track:
    return Track(query=title, title=title, requested_by=user)


def test_shuffle_empty_queue_returns_zero():
    q = TrackQueue()
    assert q.shuffle() == 0


def test_shuffle_empty_queue_no_error():
    q = TrackQueue()
    q.shuffle()  # must not raise
    assert q.is_empty()


def test_shuffle_single_track_returns_one():
    q = TrackQueue()
    q.add(_make_track("a"))
    assert q.shuffle() == 1


def test_shuffle_single_track_still_present():
    q = TrackQueue()
    t = _make_track("a")
    q.add(t)
    q.shuffle()
    result = q.list()
    assert len(result) == 1
    assert result[0].title == "a"


def test_shuffle_n_tracks_returns_n():
    q = TrackQueue()
    for i in range(5):
        q.add(_make_track(str(i)))
    assert q.shuffle() == 5


def test_shuffle_preserves_all_tracks():
    q = TrackQueue()
    titles = [str(i) for i in range(10)]
    for t in titles:
        q.add(_make_track(t))
    q.shuffle()
    result_titles = sorted(t.title for t in q.list())
    assert result_titles == sorted(titles)


def test_shuffle_does_not_touch_current():
    q = TrackQueue()
    current = _make_track("current")
    q.current = current
    q.add(_make_track("a"))
    q.add(_make_track("b"))
    q.shuffle()
    assert q.current is current


def test_shuffle_does_not_touch_history():
    q = TrackQueue()
    hist_track = _make_track("hist")
    q._history.append(hist_track)
    q.add(_make_track("a"))
    q.add(_make_track("b"))
    q.shuffle()
    assert len(q._history) == 1
    assert q._history[0] is hist_track


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {fn.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
