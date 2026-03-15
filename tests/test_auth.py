"""Tests for PIN authentication."""

from app.core.auth import (
    LoginRateLimiter,
    create_session_token,
    get_pin_hash,
    is_auth_enabled,
    verify_pin,
)


def test_get_pin_hash():
    h = get_pin_hash("1234")
    assert isinstance(h, str)
    assert len(h) == 64  # SHA-256 hex


def test_verify_pin_correct():
    h = get_pin_hash("mypin")
    assert verify_pin("mypin", h) is True


def test_verify_pin_wrong():
    h = get_pin_hash("mypin")
    assert verify_pin("wrongpin", h) is False


def test_is_auth_enabled_with_pin():
    config = {"server": {"pin": get_pin_hash("1234")}}
    assert is_auth_enabled(config) is True


def test_is_auth_enabled_no_pin():
    assert is_auth_enabled({"server": {"pin": ""}}) is False
    assert is_auth_enabled({}) is False
    assert is_auth_enabled({"server": {}}) is False


def test_create_session_token():
    t1 = create_session_token()
    t2 = create_session_token()
    assert isinstance(t1, str)
    assert len(t1) > 20
    assert t1 != t2


def test_rate_limiter_allows_initial():
    limiter = LoginRateLimiter(max_attempts=3, lockout_seconds=60)
    assert limiter.is_locked("1.2.3.4") is False


def test_rate_limiter_locks_after_max():
    limiter = LoginRateLimiter(max_attempts=3, lockout_seconds=60)
    for _ in range(3):
        limiter.record_attempt("1.2.3.4")
    assert limiter.is_locked("1.2.3.4") is True


def test_rate_limiter_reset():
    limiter = LoginRateLimiter(max_attempts=3, lockout_seconds=60)
    for _ in range(3):
        limiter.record_attempt("1.2.3.4")
    limiter.reset("1.2.3.4")
    assert limiter.is_locked("1.2.3.4") is False


def test_rate_limiter_different_ips():
    limiter = LoginRateLimiter(max_attempts=3, lockout_seconds=60)
    for _ in range(3):
        limiter.record_attempt("1.2.3.4")
    assert limiter.is_locked("1.2.3.4") is True
    assert limiter.is_locked("5.6.7.8") is False
