"""Simple in-memory per-IP sliding window rate limiter (stdlib only)."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict


class RateLimiter:
    """Sliding window rate limiter.

    Args:
        max_requests: Maximum requests allowed in the window.
        window_seconds: Window size in seconds.
    """

    def __init__(self, max_requests: int = 10, window_seconds: float = 60.0, max_keys: int = 4096) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        # OrderedDict so we can evict least-recently-touched keys (move_to_end on
        # each touch) — max_keys is then a HARD ceiling, not just a stale-only
        # sweep that frees nothing under a flood of fresh distinct keys.
        self._requests: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> bool:
        """Return True if the request is allowed, False if rate limited."""
        now = time.monotonic()
        cutoff = now - self.window_seconds

        with self._lock:
            # Use .get so merely reading an unknown key doesn't create an entry.
            timestamps = [t for t in self._requests.get(key, ()) if t > cutoff]

            if len(timestamps) >= self.max_requests:
                self._requests[key] = timestamps
                self._requests.move_to_end(key)
                return False

            timestamps.append(now)
            self._requests[key] = timestamps
            self._requests.move_to_end(key)  # mark recently-used (LRU order)
            # Bound the map: drop fully-stale keys, then — if a flood of fresh
            # distinct keys means eviction freed nothing — drop the least-
            # recently-touched keys so the map can never grow past max_keys.
            if len(self._requests) > self.max_keys:
                self._evict_stale(cutoff)
                while len(self._requests) > self.max_keys:
                    self._requests.popitem(last=False)
            return True

    def _evict_stale(self, cutoff: float) -> None:
        stale = [k for k, ts in self._requests.items() if not any(t > cutoff for t in ts)]
        for k in stale:
            del self._requests[k]

    def reset(self) -> None:
        """Clear all tracking data (useful for testing)."""
        with self._lock:
            self._requests.clear()


# Shared limiter for draft generation endpoints
draft_limiter = RateLimiter(max_requests=10, window_seconds=60.0)


RATE_LIMIT_RESPONSE = {"detail": "Rate limit exceeded. Max 10 drafts/minute."}

# Process-global cap on CONCURRENT draft generations. Each generate_draft shells
# out to a claude/mlx subprocess; the review-queue endpoints fan out up to
# batch_size of them per request, and the per-request ThreadPoolExecutor only
# bounds fan-out WITHIN one request. This semaphore bounds it across ALL requests
# + endpoints, so a flood can't spawn unbounded subprocesses and exhaust the
# shared sync threadpool. Acquire it around each generate_draft call.
draft_concurrency = threading.BoundedSemaphore(6)
