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
    def __init__(self, path: str, cookies: dict[str, str]):
        self.url = _FakeURL(path)
        self.cookies = cookies


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
