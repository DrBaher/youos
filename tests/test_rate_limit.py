"""Tests for the in-memory rate limiter."""

from app.core.rate_limit import RateLimiter


def test_allows_under_limit():
    rl = RateLimiter(max_requests=3, window_seconds=60.0)
    assert rl.is_allowed("ip1") is True
    assert rl.is_allowed("ip1") is True
    assert rl.is_allowed("ip1") is True


def test_blocks_over_limit():
    rl = RateLimiter(max_requests=3, window_seconds=60.0)
    for _ in range(3):
        rl.is_allowed("ip1")
    assert rl.is_allowed("ip1") is False


def test_separate_keys():
    rl = RateLimiter(max_requests=2, window_seconds=60.0)
    assert rl.is_allowed("ip1") is True
    assert rl.is_allowed("ip1") is True
    assert rl.is_allowed("ip1") is False
    # Different key should still be allowed
    assert rl.is_allowed("ip2") is True


def test_reset():
    rl = RateLimiter(max_requests=1, window_seconds=60.0)
    assert rl.is_allowed("ip1") is True
    assert rl.is_allowed("ip1") is False
    rl.reset()
    assert rl.is_allowed("ip1") is True


def test_window_expiry(monkeypatch):
    """Requests outside the window should not count."""
    import time

    rl = RateLimiter(max_requests=2, window_seconds=1.0)
    assert rl.is_allowed("ip1") is True
    assert rl.is_allowed("ip1") is True
    assert rl.is_allowed("ip1") is False

    # Simulate time passing beyond the window
    original_monotonic = time.monotonic
    offset = 2.0

    def shifted_monotonic():
        return original_monotonic() + offset

    monkeypatch.setattr(time, "monotonic", shifted_monotonic)
    assert rl.is_allowed("ip1") is True


def test_unknown_key_lookup_does_not_grow_map():
    """is_allowed must not create entries just by being asked about a key."""
    rl = RateLimiter(max_requests=2, window_seconds=60.0)
    rl.is_allowed("ip1")
    assert len(rl._requests) == 1  # only the key that actually made a request


def test_stale_keys_are_evicted_when_over_capacity(monkeypatch):
    """Once past max_keys, keys whose entries have all expired are dropped."""
    import time

    rl = RateLimiter(max_requests=2, window_seconds=1.0, max_keys=5)
    for i in range(5):
        rl.is_allowed(f"old-{i}")
    assert len(rl._requests) == 5

    # Advance past the window so the old entries are stale, then exceed max_keys.
    original_monotonic = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: original_monotonic() + 5.0)
    rl.is_allowed("fresh")  # triggers eviction since map now exceeds max_keys
    assert "fresh" in rl._requests
    assert all(not k.startswith("old-") for k in rl._requests)
