"""b150: Host-header (DNS-rebinding) guard + global request-body size limit."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import _host_allowed, app

client = TestClient(app)


def _req(host, method="GET"):
    return SimpleNamespace(
        headers=({"host": host} if host is not None else {}),
        method=method,
    )


def test_host_allowed_blocks_dns_rebinding():
    cfg = {"server": {"host": "127.0.0.1"}}
    assert _host_allowed(_req("127.0.0.1:8765"), cfg)
    assert _host_allowed(_req("localhost:8765"), cfg)
    assert _host_allowed(_req("testserver"), cfg)          # Starlette TestClient
    assert not _host_allowed(_req("evil.com:8765"), cfg)   # the rebinding host
    # a bind-all host can't be enumerated → skip (exposed mode relies on PIN/Origin)
    assert _host_allowed(_req("anything.example"), {"server": {"host": "0.0.0.0"}})


def test_missing_host_gated_by_method_b239():
    """b239: a missing Host header (hand-crafted raw request — uvicorn accepts
    Host-less HTTP/1.x) may read but not change state on a specific bind."""
    cfg = {"server": {"host": "127.0.0.1"}}
    for safe in ("GET", "HEAD", "OPTIONS", "get"):
        assert _host_allowed(_req(None, method=safe), cfg)
    for unsafe in ("POST", "PUT", "PATCH", "DELETE"):
        assert not _host_allowed(_req(None, method=unsafe), cfg)
    # bind-all stays unguarded regardless of method (can't enumerate hosts)
    assert _host_allowed(_req(None, method="POST"), {"server": {"host": "0.0.0.0"}})


def test_foreign_host_is_rejected_end_to_end():
    assert client.get("/healthz").status_code == 200                      # Host=testserver
    assert client.get("/healthz", headers={"host": "evil.com"}).status_code == 421


def test_oversized_request_body_is_rejected():
    big = "A" * 2_200_000  # > 2 MB
    assert client.post("/api/config/set", json={"key": "x", "value": big}).status_code == 413
    # a normal-size body is not body-blocked (unknown flag → 400, not 413)
    assert client.post("/api/config/set", json={"key": "generation.bogus", "value": True}).status_code != 413
