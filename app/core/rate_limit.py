"""Simple in-memory per-IP sliding window rate limiter (stdlib only)."""

from __future__ import annotations

import threading
import time
from collections import defaultdict


class RateLimiter:
    """Sliding window rate limiter.

    Args:
        max_requests: Maximum requests allowed in the window.
        window_seconds: Window size in seconds.
    """

    def __init__(self, max_requests: int = 10, window_seconds: float = 60.0) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> bool:
        """Return True if the request is allowed, False if rate limited."""
        now = time.monotonic()
        cutoff = now - self.window_seconds

        with self._lock:
            timestamps = self._requests[key]
            # Prune expired entries
            self._requests[key] = [t for t in timestamps if t > cutoff]
            timestamps = self._requests[key]

            if len(timestamps) >= self.max_requests:
                return False

            timestamps.append(now)
            return True

    def reset(self) -> None:
        """Clear all tracking data (useful for testing)."""
        with self._lock:
            self._requests.clear()


# Shared limiter for draft generation endpoints
draft_limiter = RateLimiter(max_requests=10, window_seconds=60.0)


RATE_LIMIT_RESPONSE = {"detail": "Rate limit exceeded. Max 10 drafts/minute."}
