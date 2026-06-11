"""b165: the Origin allowlist must track the ACTUALLY-served port.

The prod launcher (scripts/run_youos.sh) binds uvicorn on YOUOS_PORT (default
8765), but ``compute_allowed_origins`` used to read config ``server.port``
(default 8901). The moment a PIN was set, the served origin
(http://127.0.0.1:8765) was absent from the allowlist computed for 8901, so
EVERY authenticated same-origin POST was 403'd "origin not allowed" — pushing
users to disable the very control protecting an exposed deployment.

These tests pin the fix: the served port (YOUOS_PORT) is the single source of
truth for the allowlist; a PIN-set-but-bind-origin-absent misconfig warns
loudly at startup; a same-origin POST on the served port is allowed under a
PIN; and a cross-origin POST is still rejected.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.core.auth import (
    bind_origin,
    compute_allowed_origins,
    detect_served_port_from_argv,
    origin_self_check_warning,
)
from app.core.config import DEFAULT_SERVER_PORT, resolve_server_port
from app.main import SESSION_COOKIE, PinAuthMiddleware

# ── allowlist tracks the served port ──────────────────────────────────────


def test_allowlist_uses_served_port_from_env_not_config_default(monkeypatch):
    # Launcher binds 8765 via YOUOS_PORT; config still carries the old 8901.
    monkeypatch.setenv("YOUOS_PORT", "8765")
    origins = compute_allowed_origins({"server": {"port": 8901, "pin": "x"}})
    assert "http://127.0.0.1:8765" in origins
    assert "http://localhost:8765" in origins
    # The stale config port must NOT be what the allowlist is keyed on.
    assert "http://127.0.0.1:8901" not in origins


def test_resolve_server_port_precedence(monkeypatch):
    monkeypatch.setenv("YOUOS_PORT", "8765")
    assert resolve_server_port({"server": {"port": 8901}}) == 8765
    monkeypatch.delenv("YOUOS_PORT", raising=False)
    assert resolve_server_port({"server": {"port": 8901}}) == 8901
    assert resolve_server_port({}) == DEFAULT_SERVER_PORT


def test_default_matches_launcher(monkeypatch):
    # Single source of truth: no env + no config -> the launcher's default port.
    monkeypatch.delenv("YOUOS_PORT", raising=False)
    assert resolve_server_port({}) == 8765


def test_malformed_env_port_falls_back(monkeypatch):
    monkeypatch.setenv("YOUOS_PORT", "not-a-port")
    # Must not crash; falls through to config, then default.
    assert resolve_server_port({"server": {"port": 8901}}) == 8901


# ── startup self-check ─────────────────────────────────────────────────────


def test_self_check_warns_when_pin_set_but_bind_origin_absent(monkeypatch):
    # The realistic divergence the audit flagged: the process is actually bound
    # on a port (uvicorn --port) the allowlist was NOT computed for. Here the
    # allowlist resolves to 8765 but uvicorn was launched --port 9001.
    monkeypatch.setenv("YOUOS_PORT", "8765")
    cfg = {"server": {"pin": "hashed", "port": 8765, "host": "127.0.0.1"}}
    warning = origin_self_check_warning(cfg, served_port=9001)
    assert warning is not None
    assert "9001" in warning
    assert "403" in warning
    assert bind_origin(cfg, port=9001) == "http://127.0.0.1:9001"


def test_self_check_detects_served_port_from_argv():
    # The lifespan derives the real served port from uvicorn's --port argument.
    assert detect_served_port_from_argv(["uvicorn", "app.main:app", "--port", "8765"]) == 8765
    assert detect_served_port_from_argv(["uvicorn", "--port=9001"]) == 9001
    assert detect_served_port_from_argv(["uvicorn", "app.main:app"]) is None
    assert detect_served_port_from_argv(["uvicorn", "--port", "junk"]) is None


def test_self_check_silent_when_no_pin(monkeypatch):
    monkeypatch.setenv("YOUOS_PORT", "8765")
    # No PIN -> allowlist isn't enforced -> no warning even on a mismatched port.
    assert origin_self_check_warning({"server": {"port": 8765}}, served_port=9001) is None


def test_self_check_silent_when_bind_origin_in_allowlist(monkeypatch):
    monkeypatch.setenv("YOUOS_PORT", "8765")
    cfg = {"server": {"pin": "hashed", "host": "127.0.0.1"}}
    # Served port matches the allowlist port -> healthy -> silent.
    assert origin_self_check_warning(cfg, served_port=8765) is None


# ── end-to-end through the middleware ──────────────────────────────────────


class _FakeURL:
    def __init__(self, path: str):
        self.path = path


class _FakeRequest:
    def __init__(self, path, cookies, *, method="GET", headers=None):
        self.url = _FakeURL(path)
        self.cookies = cookies
        self.method = method
        # Real clients always send Host (HTTP/1.1 requires it); without one a
        # state-changing request is 421'd by the b239 missing-Host gate before
        # reaching the Origin checks under test here.
        self.headers = {"host": "127.0.0.1:8765", **(headers or {})}


def _dispatch(mw, request):
    async def call_next(_req):
        return "PASS"

    return asyncio.run(mw.dispatch(request, call_next))


def _mw_on_served_port(monkeypatch, port="8765"):
    monkeypatch.setenv("YOUOS_PORT", port)
    # Stale config port (8901) — the allowlist must still track the served port.
    mw = PinAuthMiddleware(app=None, config={"server": {"pin": "hashed", "port": 8901}})
    mw.sessions = {"good": time.time()}
    return mw


def test_same_origin_post_on_served_port_allowed_under_pin(monkeypatch):
    mw = _mw_on_served_port(monkeypatch, "8765")
    req = _FakeRequest(
        "/feedback/submit",
        {SESSION_COOKIE: "good"},
        method="POST",
        headers={"origin": "http://127.0.0.1:8765"},
    )
    assert _dispatch(mw, req) == "PASS"


def test_stale_config_port_origin_rejected(monkeypatch):
    # A POST claiming the OLD config port (8901) is now cross-origin -> blocked.
    mw = _mw_on_served_port(monkeypatch, "8765")
    req = _FakeRequest(
        "/feedback/submit",
        {SESSION_COOKIE: "good"},
        method="POST",
        headers={"origin": "http://127.0.0.1:8901"},
    )
    result = _dispatch(mw, req)
    assert getattr(result, "status_code", None) == 403


def test_cross_origin_post_still_rejected_under_pin(monkeypatch):
    mw = _mw_on_served_port(monkeypatch, "8765")
    req = _FakeRequest(
        "/feedback/submit",
        {SESSION_COOKIE: "good"},
        method="POST",
        headers={"origin": "https://evil.example"},
    )
    result = _dispatch(mw, req)
    assert getattr(result, "status_code", None) == 403


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
