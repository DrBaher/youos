"""b155: bound the auth maps — session count cap + in-memory prune on mint, and
a rate limit on the API-token mint endpoint."""

from __future__ import annotations

import time
import types
from collections import OrderedDict

import pytest
from fastapi import HTTPException

from app.core.auth import MAX_SESSIONS, load_sessions, persist_new_session


def test_persist_new_session_caps_disk_count(tmp_path):
    path = tmp_path / "sessions.json"
    for i in range(MAX_SESSIONS + 50):
        persist_new_session(f"tok-{i}", path)
    assert len(load_sessions(path)) == MAX_SESSIONS  # file count-bounded


def test_register_session_sweeps_expired_and_caps(monkeypatch):
    """The in-memory dict that authorizes requests never re-seeds from disk, so
    register_session must drop expired entries and bound the count on every mint."""
    from app.main import SESSION_MAX_AGE, PinAuthMiddleware

    mw = PinAuthMiddleware(app=None, config={"server": {"pin": "hashed"}})
    now = time.time()
    seeded: OrderedDict[str, float] = OrderedDict()
    seeded["expired"] = now - SESSION_MAX_AGE - 10
    for i in range(MAX_SESSIONS + 20):
        seeded[f"live-{i}"] = now
    mw.sessions = seeded

    mw.register_session("fresh")

    assert "fresh" in mw.sessions
    assert "expired" not in mw.sessions        # swept on insert (was never pruned)
    assert len(mw.sessions) <= MAX_SESSIONS     # bounded, oldest evicted FIFO


def test_api_token_mint_is_rate_limited(monkeypatch):
    """b155: POST /api/token throttles per IP so a looping client can't grow
    api_tokens.json / amplify the verify cost."""
    import app.api.stats_routes as sr
    from app.core.auth import LoginRateLimiter

    # fresh limiter + stub mint so we never touch the real var dir
    monkeypatch.setattr(sr, "_token_mint_limiter", LoginRateLimiter(max_attempts=5, lockout_seconds=60))
    monkeypatch.setattr("app.core.auth.add_api_token", lambda: "stub-token")

    req = types.SimpleNamespace(client=types.SimpleNamespace(host="9.9.9.9"))

    for _ in range(5):
        assert sr.create_token(req) == {"token": "stub-token"}

    with pytest.raises(HTTPException) as exc:
        sr.create_token(req)
    assert exc.value.status_code == 429
