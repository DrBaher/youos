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
