"""Regression tests for server-side session expiry in PinAuthMiddleware.

Previously the middleware stored only session token keys in an in-memory set and
never checked age, so a captured token replayed successfully until the process
restarted (ignoring SESSION_MAX_AGE). These tests pin the age check.
"""

from __future__ import annotations

import asyncio
import time

from app.main import SESSION_COOKIE, SESSION_MAX_AGE, PinAuthMiddleware


class _FakeURL:
    def __init__(self, path: str):
        self.path = path


class _FakeRequest:
    def __init__(
        self,
        path: str,
        cookies: dict[str, str],
        headers: dict[str, str] | None = None,
        method: str = "GET",
    ):
        self.url = _FakeURL(path)
        self.cookies = cookies
        # Starlette headers are case-insensitive; the middleware looks up
        # lowercase keys, so tests pass lowercase header names.
        self.headers = headers or {}
        self.method = method


def _make_middleware() -> PinAuthMiddleware:
    # A non-empty pin enables auth so dispatch enforces sessions.
    return PinAuthMiddleware(app=None, config={"server": {"pin": "hashed-pin"}})


def _dispatch(mw: PinAuthMiddleware, request: _FakeRequest):
    async def call_next(_req):
        return "PASS"

    return asyncio.run(mw.dispatch(request, call_next))


def test_fresh_token_is_accepted():
    mw = _make_middleware()
    mw.sessions = {"good": time.time()}
    result = _dispatch(mw, _FakeRequest("/feedback", {SESSION_COOKIE: "good"}))
    assert result == "PASS"


def test_expired_token_is_rejected_and_evicted():
    mw = _make_middleware()
    mw.sessions = {"old": time.time() - SESSION_MAX_AGE - 1}
    result = _dispatch(mw, _FakeRequest("/feedback", {SESSION_COOKIE: "old"}))
    # Redirect to login (303), not the protected response.
    assert getattr(result, "status_code", None) == 303
    # And the stale token is removed so it can't be reused.
    assert "old" not in mw.sessions


def test_unknown_token_is_rejected():
    mw = _make_middleware()
    mw.sessions = {}
    result = _dispatch(mw, _FakeRequest("/feedback", {SESSION_COOKIE: "nope"}))
    assert getattr(result, "status_code", None) == 303


def test_valid_api_token_header_is_accepted(monkeypatch):
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "verify_api_token", lambda t: t == "good-token")
    mw = _make_middleware()
    mw.sessions = {}
    req = _FakeRequest("/feedback", {}, headers={"x-youos-token": "good-token"})
    assert _dispatch(mw, req) == "PASS"


def test_valid_bearer_token_is_accepted(monkeypatch):
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "verify_api_token", lambda t: t == "good-token")
    mw = _make_middleware()
    mw.sessions = {}
    req = _FakeRequest("/feedback", {}, headers={"authorization": "Bearer good-token"})
    assert _dispatch(mw, req) == "PASS"


def test_invalid_api_token_header_is_rejected(monkeypatch):
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "verify_api_token", lambda t: False)
    mw = _make_middleware()
    mw.sessions = {}
    req = _FakeRequest("/feedback", {}, headers={"x-youos-token": "bad"})
    assert getattr(_dispatch(mw, req), "status_code", None) == 303
