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


class TrackQueue:
    def __init__(self):
        self._queue: deque[Track] = deque()
        self._history = []
        self.current: Track | None = None
        self.fair_play: bool = True
        self.last_played_user: str | None = None

    def add(self, track: Track):
        self._queue.append(track)

    def next(self) -> Track | None:
        if not self._queue:
            self.current = None
            return None

        curr = self.current
        if curr is not None:
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
