from __future__ import annotations

from collections import OrderedDict, deque
import math
import threading
import time
from typing import Callable


class FailedAuthThrottle:
    """Thread-safe per-client limiter for *failed* authentication attempts.

    Only failures are recorded, so valid credentials never consume budget. Once
    a client accumulates ``max_failures`` failures inside ``window_seconds`` it
    is locked out until its oldest failure leaves the window. The table is
    bounded: the least recently active clients are evicted first. A successful
    login forgets that client's failures. ``max_failures=0`` disables the
    limiter entirely.
    """

    def __init__(
        self,
        *,
        max_failures: int = 10,
        window_seconds: float = 60.0,
        max_entries: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_failures = max(0, int(max_failures))
        self.window_seconds = float(window_seconds)
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()

    def _prune(self, key: str, now: float) -> deque[float] | None:
        attempts = self._failures.get(key)
        if attempts is None:
            return None
        cutoff = now - self.window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if not attempts:
            del self._failures[key]
            return None
        return attempts

    def retry_after(self, key: str) -> int:
        """Return seconds until ``key`` may try again; ``0`` when not locked out."""

        if not self.enabled:
            return 0
        with self._lock:
            now = self._clock()
            attempts = self._prune(key, now)
            if attempts is None or len(attempts) < self.max_failures:
                return 0
            return max(1, math.ceil(attempts[0] + self.window_seconds - now))

    @property
    def enabled(self) -> bool:
        return self.max_failures > 0

    def record_success(self, key: str) -> None:
        """Forget the failures of ``key`` after it authenticated correctly."""

        with self._lock:
            self._failures.pop(key, None)

    def record_failure(self, key: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            now = self._clock()
            attempts = self._prune(key, now)
            if attempts is None:
                attempts = self._failures[key] = deque()
            attempts.append(now)
            # Keep memory bounded even for one noisy client.
            while len(attempts) > self.max_failures:
                attempts.popleft()
            self._failures.move_to_end(key)
            while len(self._failures) > self.max_entries:
                self._failures.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._failures.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._failures)
