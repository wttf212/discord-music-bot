from collections import deque
from dataclasses import dataclass


@dataclass
class Track:
    query: str
    title: str
    requested_by: str


class TrackQueue:
    def __init__(self):
        self._queue: deque[Track] = deque()
        self._history: list[Track] = []
        self.current: Track | None = None

    def add(self, track: Track):
        self._queue.append(track)

    def next(self) -> Track | None:
        curr = self.current
        if curr is not None:
            self._history.append(curr)
        self.current = self._queue.popleft() if self._queue else None
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

    def list(self) -> list[Track]:
        return list(self._queue)

    def is_empty(self) -> bool:
        return len(self._queue) == 0
