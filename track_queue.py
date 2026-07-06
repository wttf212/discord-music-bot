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


class TrackQueue:
    # Loop modes: "off" (default), "track" (repeat current), "queue" (cycle all).
    LOOP_MODES = ("off", "track", "queue")

    def __init__(self):
        self._queue: deque[Track] = deque()
        self._history = []
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

        curr = self.current
        if curr is not None and not curr.is_radio:
            if self.loop_mode == "queue":
                self._queue.append(curr)  # cycle the finished track to the back
            else:
                self._history.append(curr)

        if self.fair_play and self.last_played_user is not None and len(self._queue) > 1:
            # Find the first track by a DIFFERENT user than last_played_user
            next_idx = 0
            for i, track in enumerate(self._queue):
                if track.requested_by != self.last_played_user:
                    next_idx = i
                    break
            
            # Move track to front if a different user was found
            if next_idx > 0:
                track_to_play = self._queue[next_idx]
                self._queue.remove(track_to_play) # Deque remove is safer for types
                self._queue.appendleft(track_to_play)

        self.current = self._queue.popleft()
        self.last_played_user = self.current.requested_by if self.current else None
        return self.current

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

    def move(self, src: int, dst: int) -> Track | None:
        """Move the 1-based src-th pending track to the 1-based dst position."""
        n = len(self._queue)
        if not (1 <= src <= n and 1 <= dst <= n):
            return None
        track = self._queue[src - 1]
        del self._queue[src - 1]
        self._queue.insert(dst - 1, track)
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

    def shuffle(self) -> int:
        """Randomise pending tracks in-place. Returns count of shuffled tracks.
        Does not affect the currently-playing track (self.current) or history."""
        items = list(self._queue)
        random.shuffle(items)
        self._queue = deque(items)
        return len(items)

    def preview_fair_order(self, limit: int = 10):
        """Return up to 'limit' tracks in predicted fair-play order without mutating state.
           Capped to prevent O(N^2) CPU locks causing interaction timeouts.
        """
        if not self.fair_play or len(self._queue) <= 1:
            q_list = list(self._queue)
            return q_list[:limit]

        remaining = list(self._queue)
        result = []
        last_user = self.last_played_user

        while remaining and len(result) < limit:
            # Find first track by a different user
            chosen_idx = 0
            if last_user is not None:
                for i, t in enumerate(remaining):
                    if t.requested_by != last_user:
                        chosen_idx = i
                        break
            chosen = remaining.pop(chosen_idx)
            result.append(chosen)
            last_user = chosen.requested_by

        return result

    def is_empty(self) -> bool:
        return len(self._queue) == 0
