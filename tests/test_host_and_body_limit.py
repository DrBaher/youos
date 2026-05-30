"""b150: Host-header (DNS-rebinding) guard + global request-body size limit."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import _host_allowed, app

client = TestClient(app)


def _req(host):
    return SimpleNamespace(headers=({"host": host} if host is not None else {}))


def test_host_allowed_blocks_dns_rebinding():
    cfg = {"server": {"host": "127.0.0.1"}}
    assert _host_allowed(_req("127.0.0.1:8765"), cfg)
    assert _host_allowed(_req("localhost:8765"), cfg)
    assert _host_allowed(_req("testserver"), cfg)          # Starlette TestClient
    assert not _host_allowed(_req("evil.com:8765"), cfg)   # the rebinding host
    assert _host_allowed(_req(None), cfg)                   # missing Host = non-browser, not the threat
    # a bind-all host can't be enumerated → skip (exposed mode relies on PIN/Origin)
    assert _host_allowed(_req("anything.example"), {"server": {"host": "0.0.0.0"}})


def test_foreign_host_is_rejected_end_to_end():
    assert client.get("/healthz").status_code == 200                      # Host=testserver
    assert client.get("/healthz", headers={"host": "evil.com"}).status_code == 421


def test_oversized_request_body_is_rejected():
    big = "A" * 2_200_000  # > 2 MB
    assert client.post("/api/config/set", json={"key": "x", "value": big}).status_code == 413
    # a normal-size body is not body-blocked (unknown flag → 400, not 413)
    assert client.post("/api/config/set", json={"key": "generation.bogus", "value": True}).status_code != 413
