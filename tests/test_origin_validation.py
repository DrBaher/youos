"""CSRF / Origin allowlisting for cookie-authenticated state-changing requests.

Deferred from PR #2 (security hardening) because the original Gmail
bookmarklet called the server cross-origin from gmail.com. The bookmarklet
has been replaced by the browser extension (PR #3) which now authenticates
via an API token instead of the session cookie (PR #4) — so the cookie's
CSRF surface can finally be tightened without breaking the extension.

These tests pin the resulting policy:

- State-changing methods (POST / PUT / DELETE / PATCH) authed by the session
  cookie require an Origin (or Referer) that matches the server's own origin
  or a configured allowlist entry.
- GET / HEAD / OPTIONS are not CSRF targets and never get blocked here.
- Requests authed by API token bypass the origin check entirely.
- Login / static prefixes are skipped just like before (the login POST is
  same-origin from the rendered login page; protecting it here would mostly
  guard against login-CSRF which is moot for a single-user instance).
"""

from __future__ import annotations

import asyncio
import time

from app.core.auth import (
    compute_allowed_origins,
    request_origin_allowed,
)
from app.main import SESSION_COOKIE, PinAuthMiddleware

# ── unit: compute_allowed_origins ────────────────────────────────────────

def test_allowed_origins_includes_loopback_for_default_config():
    origins = compute_allowed_origins({"server": {"port": 8901}})
    assert "http://127.0.0.1:8901" in origins
    assert "http://localhost:8901" in origins


def test_allowed_origins_adds_lan_host_when_non_loopback():
    origins = compute_allowed_origins({"server": {"host": "192.168.1.20", "port": 8765}})
    assert "http://192.168.1.20:8765" in origins
    # Loopback variants still included so the local UI keeps working.
    assert "http://127.0.0.1:8765" in origins


def test_allowed_origins_includes_tailscale_when_set():
    origins = compute_allowed_origins(
        {"server": {"port": 8901}, "tailscale": {"hostname": "my-mac"}},
    )
    assert "https://my-mac.ts.net" in origins
    assert "http://my-mac.ts.net" in origins


def test_allowed_origins_appends_user_extras():
    origins = compute_allowed_origins(
        {"server": {"port": 8901, "allowed_origins": ["chrome-extension://abcdef/"]}},
    )
    # Trailing slash stripped so the equality check is robust.
    assert "chrome-extension://abcdef" in origins


def test_allowed_origins_ignores_blank_or_non_string_extras():
    origins = compute_allowed_origins(
        {"server": {"port": 8901, "allowed_origins": ["", "  ", 123, None, "https://ok.example"]}},
    )
    assert "https://ok.example" in origins
    assert "" not in origins
    assert "  " not in origins


# ── unit: request_origin_allowed ─────────────────────────────────────────

ALLOWED = {"http://127.0.0.1:8901", "https://my-mac.ts.net"}


def test_get_requests_always_pass():
    """GET is not a CSRF target — no Origin check."""
    assert request_origin_allowed(
        method="GET", origin="https://evil.example", referer=None, allowed_origins=ALLOWED,
    )


def test_post_with_matching_origin_passes():
    assert request_origin_allowed(
        method="POST",
        origin="http://127.0.0.1:8901",
        referer=None,
        allowed_origins=ALLOWED,
    )


def test_post_with_mismatched_origin_is_rejected():
    assert not request_origin_allowed(
        method="POST",
        origin="https://evil.example",
        referer=None,
        allowed_origins=ALLOWED,
    )


def test_post_with_null_origin_is_rejected():
    """Sandboxed / file:// contexts send `Origin: null` — never allowed."""
    assert not request_origin_allowed(
        method="POST", origin="null", referer=None, allowed_origins=ALLOWED,
    )


def test_post_with_missing_origin_falls_back_to_referer():
    assert request_origin_allowed(
        method="POST",
        origin=None,
        referer="http://127.0.0.1:8901/feedback",
        allowed_origins=ALLOWED,
    )


def test_post_with_missing_origin_and_mismatched_referer_is_rejected():
    assert not request_origin_allowed(
        method="POST",
        origin=None,
        referer="https://evil.example/page",
        allowed_origins=ALLOWED,
    )


def test_post_with_no_origin_or_referer_is_rejected():
    assert not request_origin_allowed(
        method="POST", origin=None, referer=None, allowed_origins=ALLOWED,
    )


def test_referer_prefix_must_be_origin_plus_slash():
    """Guard against `http://127.0.0.1:8901.evil.com/...` slipping through."""
    assert not request_origin_allowed(
        method="POST",
        origin=None,
        referer="http://127.0.0.1:8901.evil.com/path",
        allowed_origins=ALLOWED,
    )


def test_put_and_delete_and_patch_are_also_state_changing():
    for method in ("PUT", "DELETE", "PATCH"):
        assert not request_origin_allowed(
            method=method,
            origin="https://evil.example",
            referer=None,
            allowed_origins=ALLOWED,
        )


# ── integration: PinAuthMiddleware ───────────────────────────────────────

class _FakeURL:
    def __init__(self, path: str):
        self.path = path


class _FakeRequest:
    def __init__(
        self,
        path: str,
        cookies: dict[str, str],
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
    ):
        self.url = _FakeURL(path)
        self.cookies = cookies
        self.method = method
        self.headers = headers or {}


def _dispatch(mw: PinAuthMiddleware, request: _FakeRequest):
    async def call_next(_req):
        return "PASS"

    return asyncio.run(mw.dispatch(request, call_next))


def _make_middleware(*, port: int = 8901) -> PinAuthMiddleware:
    return PinAuthMiddleware(
        app=None,
        config={"server": {"pin": "hashed-pin", "port": port}},
    )


def test_cookie_authed_get_passes_without_origin():
    mw = _make_middleware()
    mw.sessions = {"good": time.time()}
    result = _dispatch(mw, _FakeRequest("/feedback", {SESSION_COOKIE: "good"}, method="GET"))
    assert result == "PASS"


def test_cookie_authed_post_with_same_origin_passes():
    mw = _make_middleware()
    mw.sessions = {"good": time.time()}
    req = _FakeRequest(
        "/feedback/submit",
        {SESSION_COOKIE: "good"},
        method="POST",
        headers={"origin": "http://127.0.0.1:8901"},
    )
    assert _dispatch(mw, req) == "PASS"


def test_cookie_authed_post_with_evil_origin_returns_403():
    mw = _make_middleware()
    mw.sessions = {"good": time.time()}
    req = _FakeRequest(
        "/feedback/submit",
        {SESSION_COOKIE: "good"},
        method="POST",
        headers={"origin": "https://evil.example"},
    )
    result = _dispatch(mw, req)
    assert getattr(result, "status_code", None) == 403


def test_cookie_authed_post_without_origin_or_referer_returns_403():
    """A browser would always send Origin on a cross-origin POST — its absence
    on a cookie-authed write is treated as suspect, not silently allowed."""
    mw = _make_middleware()
    mw.sessions = {"good": time.time()}
    req = _FakeRequest("/feedback/submit", {SESSION_COOKIE: "good"}, method="POST")
    result = _dispatch(mw, req)
    assert getattr(result, "status_code", None) == 403


def test_token_authed_post_with_any_origin_passes(monkeypatch):
    """API tokens are not CSRF-prone — no origin check is applied to them."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "verify_api_token", lambda t: t == "ext-token")
    mw = _make_middleware()
    mw.sessions = {}
    req = _FakeRequest(
        "/feedback/generate",
        {},
        method="POST",
        headers={
            "x-youos-token": "ext-token",
            "origin": "chrome-extension://aabbccdd",
        },
    )
    assert _dispatch(mw, req) == "PASS"


def test_login_post_is_not_blocked_by_origin_check():
    """The middleware skips /login entirely; the route handler enforces the
    rate limiter for login-bruteforce protection."""
    mw = _make_middleware()
    mw.sessions = {}
    req = _FakeRequest("/login", {}, method="POST", headers={"origin": "https://evil.example"})
    # Skipped — the request reaches call_next without auth/origin enforcement.
    assert _dispatch(mw, req) == "PASS"


def test_disabled_auth_skips_origin_check_entirely():
    """No PIN configured → middleware is a no-op. Origin check only matters
    once auth is enabled (the no-PIN case prints a SECURITY warning at
    startup elsewhere)."""
    mw = PinAuthMiddleware(app=None, config={"server": {"pin": "", "port": 8901}})
    req = _FakeRequest("/feedback/submit", {}, method="POST", headers={"origin": "https://evil.example"})
    assert _dispatch(mw, req) == "PASS"


def test_allowed_origins_extra_entry_lets_through_cookie_authed_post():
    """`server.allowed_origins` is the escape hatch for, e.g., a reverse
    proxy or LAN host that the user wants to permit explicitly."""
    mw = PinAuthMiddleware(
        app=None,
        config={
            "server": {
                "pin": "hashed-pin",
                "port": 8901,
                "allowed_origins": ["https://proxy.example"],
            },
        },
    )
    mw.sessions = {"good": time.time()}
    req = _FakeRequest(
        "/feedback/submit",
        {SESSION_COOKIE: "good"},
        method="POST",
        headers={"origin": "https://proxy.example"},
    )
    assert _dispatch(mw, req) == "PASS"


def test_tailscale_hostname_origin_is_allowed():
    mw = PinAuthMiddleware(
        app=None,
        config={
            "server": {"pin": "hashed-pin", "port": 8901},
            "tailscale": {"hostname": "my-mac"},
        },
    )
    mw.sessions = {"good": time.time()}
    req = _FakeRequest(
        "/feedback/submit",
        {SESSION_COOKIE: "good"},
        method="POST",
        headers={"origin": "https://my-mac.ts.net"},
    )
    assert _dispatch(mw, req) == "PASS"
