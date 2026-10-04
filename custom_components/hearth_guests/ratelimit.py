"""Per-IP limiter for bad guest tokens.

Pure Python, no Home Assistant imports. Ten failures within ten minutes block the address
for fifteen minutes (API.md, "Guest API"). The clock is injectable for tests.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
import time

from .const import RATE_LIMIT_BLOCK, RATE_LIMIT_FAILURES, RATE_LIMIT_WINDOW

# Upper bound on tracked addresses, so a flood of spoofed sources cannot grow memory.
MAX_TRACKED = 4096


class BadTokenLimiter:
    """Count bad-token failures per key (an IP address) and block repeat offenders."""

    def __init__(
        self,
        *,
        max_failures: int = RATE_LIMIT_FAILURES,
        window: float = RATE_LIMIT_WINDOW,
        block: float = RATE_LIMIT_BLOCK,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize the limiter."""
        self._max_failures = max_failures
        self._window = window
        self._block = block
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._blocked_until: dict[str, float] = {}

    def is_blocked(self, key: str) -> bool:
        """True while `key` is blocked."""
        until = self._blocked_until.get(key)
        if until is None:
            return False
        if self._clock() >= until:
            del self._blocked_until[key]
            return False
        return True

    def record_failure(self, key: str) -> bool:
        """Record a failure for `key`. Returns True if `key` is now blocked."""
        now = self._clock()
        self._prune(now)
        failures = self._failures.setdefault(key, deque())
        failures.append(now)
        while failures and failures[0] <= now - self._window:
            failures.popleft()
        if len(failures) >= self._max_failures:
            self._blocked_until[key] = now + self._block
            del self._failures[key]
            return True
        return False

    def _prune(self, now: float) -> None:
        """Drop stale entries; cap the number of tracked keys."""
        cutoff = now - self._window
        for key in [k for k, q in self._failures.items() if not q or q[-1] <= cutoff]:
            del self._failures[key]
        for key in [k for k, t in self._blocked_until.items() if t <= now]:
            del self._blocked_until[key]
        while len(self._failures) >= MAX_TRACKED:
            # Forget the key whose latest failure is oldest.
            oldest = min(self._failures, key=lambda k: self._failures[k][-1])
            del self._failures[oldest]
