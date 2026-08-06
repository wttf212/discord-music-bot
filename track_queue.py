from collections import deque
from dataclasses import dataclass
import random


@dataclass
class Track:
    query: str
    title: str
    requested_by: str
    thumbnail: str = ""
    url: str = ""
    is_radio: bool = False
    # Cached full get_audio_url() result (CDN URL + metadata) from a background
    # prefetch/enqueue resolve, so the eventual play() can skip the ~1.3s resolve.
    # resolved_at is wall-clock (time.time()) for TTL fallback freshness checks.
    resolved_info: dict | None = None
    resolved_at: float = 0.0
    # True once this track has played during the current lap of loop_mode
    # "queue". Only ever consulted while looping, so loop "off"/"track"
    # ordering is unchanged. Reset when a lap rolls over and when the track is
    # selected, so a stale mark cannot survive a trip through loop "off".
    lap_played: bool = False


class TrackQueue:
    # Loop modes: "off" (default), "track" (repeat current), "queue" (cycle all).
    LOOP_MODES = ("off", "track", "queue")

    def __init__(self):
        self._queue: deque[Track] = deque()
        self._history: deque[Track] = deque(maxlen=100)
        self.current: Track | None = None
        self.fair_play: bool = True
        self.last_played_user: str | None = None
        self.loop_mode: str = "off"

    def add(self, track: Track):
        self._queue.append(track)

    def cycle_loop(self) -> str:
        """Advance loop mode off -> track -> queue -> off. Returns the new mode."""
        i = self.LOOP_MODES.index(self.loop_mode) if self.loop_mode in self.LOOP_MODES else 0
        self.loop_mode = self.LOOP_MODES[(i + 1) % len(self.LOOP_MODES)]
        return self.loop_mode

    def next(self, force: bool = False) -> Track | None:
        """Return the next track to play.

        force=True (manual skip / next button) bypasses track-loop so a user can
        always move on. force=False (natural track end / auto-next) honours the
        loop mode: 'track' replays current, 'queue' cycles the finished track to
        the back of the queue.
        """
        # Track loop: replay the current track on natural end (not on manual skip).
        if self.loop_mode == "track" and self.current is not None and not force:
            return self.current

        if not self._queue:
            # Queue loop with a single finished track: replay it.
            if self.loop_mode == "queue" and self.current is not None and not self.current.is_radio:
                return self.current
            self.current = None
            return None

        picks, lap_rolled = self._fair_sequence(1)
        chosen = picks[0]        # _queue is non-empty here, so there is always one

        curr = self.current
        if curr is not None and not curr.is_radio:
            if self.loop_mode == "queue":
                curr.lap_played = True    # it has had its turn this lap
                self._queue.append(curr)  # cycle the finished track to the back
            else:
                self._history.append(curr)
        if lap_rolled:
            for t in self._queue:
                t.lap_played = False
        self.discard(chosen)              # by identity, not value equality
        chosen.lap_played = False
        self.current = chosen
        self.last_played_user = chosen.requested_by
        return chosen

    def _lap_pool(self):
        """The candidate set next() picks from: the pending queue plus, under a
        queue loop, the finished `current` that is about to be cycled to the
        back. Marks are honoured ONLY under loop "queue" -- that is what keeps
        loop "off"/"track" ordering bit-identical to before laps existed."""
        pool = list(self._queue)
        looping = self.loop_mode == "queue"
        marks = [t.lap_played and looping for t in pool]
        if looping and self.current is not None and not self.current.is_radio:
            pool.append(self.current)
            marks.append(True)
        return pool, marks

    @staticmethod
    def _pick_index(tracks, marks, last_user, fair_play, blocked=-1):
        """Index of the track to play, or -1 if nothing is eligible.

        Fair play takes the first track by a requester other than last_user,
        else the first eligible track. Tracks already played this lap are
        ineligible; the caller rolls the lap over when none are left.
        """
        interleave = fair_play and last_user is not None
        first_unplayed = -1
        for i, t in enumerate(tracks):
            if marks[i] or i == blocked:
                continue
            if first_unplayed < 0:
                first_unplayed = i
                if not interleave:
                    return i
            if t.requested_by != last_user:
                return i
        return first_unplayed

    def _fair_sequence(self, limit):
        """The next `limit` tracks in the exact order next() will take them, plus
        whether the FIRST of them needed a new lap. Pure: mutates nothing.

        This is the ONLY ordering rule in the class. next() consumes one step and
        pops what it names; preview_fair_order() consumes `limit` steps and pops
        nothing. Neither has a rule of its own, so playback, the Up Next card and
        the CDN prefetch cannot drift apart.
        """
        pool, marks = self._lap_pool()
        looping = self.loop_mode == "queue"
        # The just-finished track sits at the tail of the pool. It must not be
        # picked as the very NEXT track (that would replay it back-to-back), but
        # it rejoins the rotation from step 1 on, exactly as next() lets it.
        blocked = len(pool) - 1 if len(pool) > len(self._queue) else -1
        last_user = self.last_played_user
        result, rolled_first = [], False
        while pool and len(result) < limit:
            if all(marks):
                if result:
                    break                 # one lap is enough for a preview
                marks = [False] * len(marks)
                rolled_first = True
            i = self._pick_index(pool, marks, last_user, self.fair_play, blocked)
            if i < 0:
                break                     # only the just-finished track is left
            chosen = pool.pop(i)
            marks.pop(i)
            blocked = -1                  # the block applies to step 0 only
            result.append(chosen)
            last_user = chosen.requested_by
            if looping and not chosen.is_radio:
                pool.append(chosen)       # cycles to the back, played this lap
                marks.append(True)
        return result, rolled_first

    def requeue_front(self, track: Track) -> None:
        """Put a track back at the head of the queue after a failed start.

        Clears `current` because this track never actually played: leaving it set would
        make the following next() file it into history (loop off), append a second copy
        to the back (loop 'queue'), or return it without popping the copy we just pushed
        (loop 'track').
        """
        self._queue.appendleft(track)
        self.current = None

    def previous(self) -> Track | None:
        if not self._history:
            return None
        curr = self.current
        if curr is not None:
            self._queue.appendleft(curr)
        self.current = self._history.pop()
        return self.current

    def clear(self):
        self._queue.clear()
        self._history.clear()
        self.current = None
        self.last_played_user = None
        self.fair_play = True
        self.loop_mode = "off"

    def remove(self, index: int) -> Track | None:
        """Remove the 1-based index-th pending track. Returns it, or None if invalid."""
        if 1 <= index <= len(self._queue):
            track = self._queue[index - 1]
            del self._queue[index - 1]
            return track
        return None

    def discard(self, track: Track) -> bool:
        """Remove this exact pending track by identity. True if it was queued.

        Index-based remove() is unusable here: the prefetch picks its target via
        preview_fair_order(), whose ordering is not the raw queue order.
        """
        for i, queued in enumerate(self._queue):
            if queued is track:
                del self._queue[i]
                return True
        return False

    def move(self, src: int, dst: int) -> Track | None:
        """Move the 1-based src-th pending track to the 1-based dst position.

        Under loop "queue" a moved track may play twice in one lap: an explicit
        user action wins over the lap bookkeeping.
        """
        n = len(self._queue)
        if not (1 <= src <= n and 1 <= dst <= n):
            return None
        track = self._queue[src - 1]
        del self._queue[src - 1]
        self._queue.insert(dst - 1, track)
        track.lap_played = False   # an explicit reposition is honoured this lap
        return track

    def skip_to(self, index: int) -> bool:
        """Drop the pending tracks before the 1-based index (to history) so it plays
        next. Returns False if index is out of range. Caller then advances playback."""
        if not (1 <= index <= len(self._queue)):
            return False
        for _ in range(index - 1):
            dropped = self._queue.popleft()
            if dropped is not None and not dropped.is_radio:
                self._history.append(dropped)
        return True

    def clear_upcoming(self) -> int:
        """Clear only the pending queue (keeps the current track playing). Returns count."""
        n = len(self._queue)
        self._queue.clear()
        return n

    def dedupe(self) -> int:
        """Remove duplicate pending tracks (same url/query), keeping the first. Returns count removed."""
        seen = set()
        result: deque[Track] = deque()
        removed = 0
        for t in self._queue:
            key = t.url or t.query
            if key in seen:
                removed += 1
                continue
            seen.add(key)
            result.append(t)
        self._queue = result
        return removed

    def list(self):
        return list(self._queue)

    def __len__(self) -> int:
        return len(self._queue)

    def __iter__(self):
        return iter(self._queue)

    def shuffle(self) -> int:
        """Randomise pending tracks in-place. Returns count of shuffled tracks.
        Does not affect the currently-playing track (self.current) or history."""
        items = list(self._queue)
        random.shuffle(items)
        self._queue = deque(items)
        for t in items:
            t.lap_played = False   # a reshuffled rotation starts a fresh lap
        return len(items)

    def preview_fair_order(self, limit: int = 10):
        """Up to 'limit' tracks in the exact order next() will play them.
           Capped to prevent O(N^2) CPU locks causing interaction timeouts.

        Under loop "queue" this models the finished track cycling to the back and
        stops after one lap, so the Up Next card and the CDN prefetch name the
        tracks playback will actually reach. The first entry is always a PENDING
        track, never self.current, so gs.queue.discard() on the prefetch target
        still matches by identity (commands.py:1918).
        """
        return self._fair_sequence(limit)[0]

    def is_empty(self) -> bool:
        return len(self._queue) == 0
