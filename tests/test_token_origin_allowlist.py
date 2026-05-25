"""Optional Origin allowlist for *token*-authenticated state-changing requests.

PR #17 added CSRF/Origin checks for the cookie auth path but deliberately
left token auth wide open — token auth isn't CSRF-prone (attacker can't
make the browser send a token they don't already know). The remaining
exposure is "compromised page exfiltrates the token, then reuses it from
any origin". This module pins the opt-in allowlist that closes that gap
without breaking existing extension installs that haven't opted in.

Policy:

- ``server.token_allowed_origins`` *not configured* (or blank list)
  → token auth from any origin succeeds (today's behavior preserved).
- ``server.token_allowed_origins: [chrome-extension://abcd]`` configured
  → token-authed POST must have ``Origin: chrome-extension://abcd``.
- GET / HEAD / OPTIONS never get blocked (no CSRF target).
- ``Origin: null`` rejected when allowlist is active.
- No Referer fallback (token clients are expected to send Origin).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.core.auth import (
    compute_token_allowed_origins,
    token_request_origin_allowed,
)
from app.main import PinAuthMiddleware

# ── compute_token_allowed_origins ─────────────────────────────────────────

def test_returns_none_when_key_absent():
    """Distinguishes "no check" from "check against empty set". Returning
    None preserves back-compat for instances that haven't opted in."""
    assert compute_token_allowed_origins({"server": {"pin": "..."}}) is None


def test_returns_none_when_list_empty():
    """A list of [] is a foot-gun ("block everything") and almost certainly
    a config typo — treat as not-configured rather than locking the user
    out of their own extension."""
    assert compute_token_allowed_origins({"server": {"token_allowed_origins": []}}) is None


def test_returns_none_when_value_not_a_list():
    """Strings, dicts, ints in the slot — fall back to not-configured
    rather than crashing the middleware at construction time."""
    for bad in ("chrome-extension://abc", {"foo": "bar"}, 42, None):
        assert compute_token_allowed_origins({"server": {"token_allowed_origins": bad}}) is None


def test_normalizes_entries_and_drops_blanks():
    """Trailing slashes get stripped; blanks/non-strings dropped. Same
    sanitisation as `compute_allowed_origins` for the cookie path."""
    origins = compute_token_allowed_origins(
        {"server": {"token_allowed_origins": [
            "chrome-extension://abc/",
            "  ",
            42,
            None,
            "moz-extension://def",
        ]}},
    )
    assert origins == {"chrome-extension://abc", "moz-extension://def"}


# ── token_request_origin_allowed ──────────────────────────────────────────

def test_unconfigured_allowlist_passes_any_origin():
    """allowed_origins=None means "not configured" — preserves the
    historical token-authenticates-anywhere behaviour. Without this,
    deploying this code would break every existing extension install."""
    assert token_request_origin_allowed(
        method="POST", origin="https://evil.example", allowed_origins=None,
    )


def test_get_passes_even_when_allowlist_active():
    """GET isn't a CSRF target; never blocked by this layer."""
    assert token_request_origin_allowed(
        method="GET",
        origin="https://evil.example",
        allowed_origins={"chrome-extension://abc"},
    )


def test_matching_origin_passes():
    assert token_request_origin_allowed(
        method="POST",
        origin="chrome-extension://abc",
        allowed_origins={"chrome-extension://abc"},
    )


def test_mismatched_origin_is_rejected():
    assert not token_request_origin_allowed(
        method="POST",
        origin="https://evil.example",
        allowed_origins={"chrome-extension://abc"},
    )


def test_missing_origin_is_rejected_when_allowlist_active():
    """Token clients (extensions, CLIs) are expected to always send Origin
    on state-changers. Absent Origin against an allowlisted instance is
    suspect — no Referer fallback (unlike the cookie path which sees
    legitimate same-origin POSTs from older browsers without Origin)."""
    assert not token_request_origin_allowed(
        method="POST",
        origin=None,
        allowed_origins={"chrome-extension://abc"},
    )


def test_null_origin_is_rejected_when_allowlist_active():
    """Sandboxed contexts → reject. Same as the cookie-path policy."""
    assert not token_request_origin_allowed(
        method="POST", origin="null", allowed_origins={"chrome-extension://abc"},
    )


def test_trailing_slash_does_not_break_match():
    """Browser-sent Origins never have a trailing slash, but document the
    invariant that any normalisation we do at config time is mirrored at
    request time."""
    assert token_request_origin_allowed(
        method="POST",
        origin="chrome-extension://abc/",
        allowed_origins={"chrome-extension://abc"},
    )


def test_state_changing_methods_are_all_checked():
    """PUT/DELETE/PATCH/POST all checked; GET/HEAD/OPTIONS not."""
    for state_changing in ("POST", "PUT", "DELETE", "PATCH"):
        assert not token_request_origin_allowed(
            method=state_changing,
            origin="https://evil.example",
            allowed_origins={"chrome-extension://abc"},
        )
    for safe in ("GET", "HEAD", "OPTIONS"):
        assert token_request_origin_allowed(
            method=safe,
            origin="https://evil.example",
            allowed_origins={"chrome-extension://abc"},
        )


# ── PinAuthMiddleware integration ─────────────────────────────────────────

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


def test_unconfigured_token_allowlist_does_not_break_existing_extensions(monkeypatch):
    """Back-compat smoke: an instance that hasn't opted into the allowlist
    must keep accepting tokens from any origin (the historical behaviour
    every shipped extension install relies on)."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "verify_api_token", lambda t: t == "good")
    mw = PinAuthMiddleware(app=None, config={"server": {"pin": "x"}})
    assert mw.token_allowed_origins is None

    req = _FakeRequest(
        "/feedback/generate", {},
        method="POST",
        headers={"x-youos-token": "good", "origin": "chrome-extension://anything"},
    )
    assert _dispatch(mw, req) == "PASS"


def test_configured_allowlist_blocks_mismatched_token_origin(monkeypatch):
    """A configured allowlist makes a mismatched token-authed POST return
    403 — distinct from the 303 used for missing/invalid auth, so the
    extension can distinguish."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "verify_api_token", lambda t: t == "good")
    mw = PinAuthMiddleware(
        app=None,
        config={"server": {"pin": "x", "token_allowed_origins": ["chrome-extension://abc"]}},
    )
    assert mw.token_allowed_origins == {"chrome-extension://abc"}

    req = _FakeRequest(
        "/feedback/generate", {},
        method="POST",
        headers={"x-youos-token": "good", "origin": "https://evil.example"},
    )
    result = _dispatch(mw, req)
    assert getattr(result, "status_code", None) == 403


def test_configured_allowlist_admits_matching_token_origin(monkeypatch):
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "verify_api_token", lambda t: t == "good")
    mw = PinAuthMiddleware(
        app=None,
        config={"server": {"pin": "x", "token_allowed_origins": ["chrome-extension://abc"]}},
    )
    req = _FakeRequest(
        "/feedback/generate", {},
        method="POST",
        headers={"x-youos-token": "good", "origin": "chrome-extension://abc"},
    )
    assert _dispatch(mw, req) == "PASS"


def test_token_get_passes_without_origin_check(monkeypatch):
    """GET is safe; allowlist doesn't apply. Otherwise a non-mutating
    extension fetch (`GET /api/config`) would 403 just for missing Origin."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "verify_api_token", lambda t: t == "good")
    mw = PinAuthMiddleware(
        app=None,
        config={"server": {"pin": "x", "token_allowed_origins": ["chrome-extension://abc"]}},
    )
    req = _FakeRequest(
        "/api/config", {},
        method="GET",
        headers={"x-youos-token": "good"},
    )
    assert _dispatch(mw, req) == "PASS"


def test_cookie_auth_path_still_uses_its_own_allowlist(monkeypatch):
    """Sanity: the two allowlists are independent. A cookie-authed POST
    with a token-allowed-but-not-cookie-allowed origin should still be
    judged by the cookie-path rule."""
    mw = PinAuthMiddleware(
        app=None,
        config={
            "server": {
                "pin": "x",
                "port": 8901,
                # Token allowlist permits the extension origin...
                "token_allowed_origins": ["chrome-extension://abc"],
                # ...but cookie allowlist is the default (loopback only).
            },
        },
    )
    mw.sessions = {"sess": time.time()}

    # Cookie-authed POST from the extension origin: should be blocked by
    # the COOKIE allowlist, not silently allowed by the token allowlist.
    req = _FakeRequest(
        "/feedback/submit",
        {"youos_session": "sess"},
        method="POST",
        headers={"origin": "chrome-extension://abc"},
    )
    result = _dispatch(mw, req)
    assert getattr(result, "status_code", None) == 403


@pytest.mark.parametrize(
    "config",
    [
        {"server": {"pin": "x", "token_allowed_origins": []}},
        {"server": {"pin": "x", "token_allowed_origins": ["", "  "]}},
        {"server": {"pin": "x", "token_allowed_origins": "not-a-list"}},
        {"server": {"pin": "x"}},  # absent entirely
    ],
)
def test_middleware_treats_blank_or_invalid_allowlist_as_unconfigured(monkeypatch, config):
    """Each of these inputs is "not really an allowlist"; the middleware
    falls back to today's anywhere-token behaviour rather than blocking
    every token request (which would be the worst possible foot-gun)."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "verify_api_token", lambda t: t == "good")
    mw = PinAuthMiddleware(app=None, config=config)
    assert mw.token_allowed_origins is None
    req = _FakeRequest(
        "/feedback/generate", {},
        method="POST",
        headers={"x-youos-token": "good", "origin": "https://anywhere.example"},
    )
    assert _dispatch(mw, req) == "PASS"
