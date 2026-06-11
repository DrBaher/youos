"""Config-write API (Config PR 2).

GET /api/config/flags lists the whitelisted flags; POST /api/config/set sets
one. Writes are restricted to the feature-flag whitelist (can't touch arbitrary
config). The settings page + onboarding wizard use these.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core import feature_flags as ff
from app.main import app

client = TestClient(app)


def test_flags_endpoint_lists_known_flags():
    body = client.get("/api/config/flags").json()
    keys = {f["key"] for f in body["flags"]}
    assert "generation.multi_candidate.enabled" in keys
    assert "ingestion.google_backend" in keys
    sample = next(f for f in body["flags"] if f["key"] == "generation.log_drafts")
    assert {"key", "label", "type", "value"} <= set(sample)


def test_set_unknown_key_is_400():
    # Real set_flag raises KeyError before any write — safe to call.
    r = client.post("/api/config/set", json={"key": "generation.bogus", "value": True})
    assert r.status_code == 400
    assert "Unknown flag" in r.json()["detail"]


def test_set_bad_value_is_400():
    # Real set_flag raises ValueError before any write — safe to call.
    r = client.post("/api/config/set", json={"key": "ingestion.google_backend", "value": "outlook"})
    assert r.status_code == 400
    assert "Invalid value" in r.json()["detail"]


def test_set_valid_flag_returns_ok(monkeypatch):
    # Mock the writer so the test doesn't mutate the real config file.
    captured = {}
    monkeypatch.setattr(
        ff, "set_flag", lambda k, v, **kw: captured.update(k=k, v=v, kw=kw) or True
    )
    r = client.post("/api/config/set", json={"key": "generation.multi_candidate.enabled", "value": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True, "key": "generation.multi_candidate.enabled", "value": True}
    assert captured["k"] == "generation.multi_candidate.enabled"
    assert captured["v"] == "true"
    assert captured["kw"] == {"allow_send_frontier": False}  # b259: network path refuses frontier flags


# --- b140: PIN-set endpoint (hashed) + helpful rejection of the flag form -----


def test_config_set_pin_endpoint_hashes_and_saves(monkeypatch):
    import app.core.config as cfgmod
    from app.core.auth import verify_pin

    saved: dict = {}
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {})
    monkeypatch.setattr(cfgmod, "save_config", lambda cfg, *a, **k: saved.update(cfg=cfg))
    r = client.post("/api/config/set-pin", json={"pin": "1234"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    stored = saved["cfg"]["server"]["pin"]
    assert stored.startswith("pbkdf2:") and verify_pin("1234", stored)  # hashed, not plaintext


def test_config_set_pin_endpoint_rejects_empty():
    r = client.post("/api/config/set-pin", json={"pin": "   "})
    assert r.status_code == 400


def test_config_set_server_pin_flag_via_api_rejected_with_hint():
    r = client.post("/api/config/set", json={"key": "server.pin", "value": "1234"})
    assert r.status_code == 400
    assert "set-pin" in r.json()["detail"]
