from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    def __init__(self, limit: int, window: float = 60.0, clock=time.monotonic):
        self._limit = limit
        self._window = window
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    _SWEEP_THRESHOLD = 1000

    def __len__(self) -> int:
        with self._lock:
            return len(self._hits)

    def _sweep(self, now: float) -> None:
        stale = [k for k, h in self._hits.items() if not h or now - h[-1] >= self._window]
        for k in stale:
            del self._hits[k]

    def allow(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            if len(self._hits) > self._SWEEP_THRESHOLD:
                self._sweep(now)
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= self._window:
                hits.popleft()
            if not hits:
                self._hits.pop(key, None)
            if len(hits) >= self._limit:
                return False
            hits.append(now)
            self._hits[key] = hits
            return True
