"""Web onboarding wizard + identity write (Config PR 4).

A comprehensive guided first-run wizard at /welcome: performs the config steps
(identity, Google backend) in-browser and guides the operational ones (ingest,
train, secure) with the command + a live readiness check. Verified
structurally; visual flow eyeballed on a running instance.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core import feature_flags as ff
from app.core.config import load_config
from app.main import app

client = TestClient(app)


# --- identity write --------------------------------------------------------


def test_set_identity_roundtrip(tmp_path):
    cfg = tmp_path / "c.yaml"
    out = ff.set_identity("Jane Doe", ["jane@work.com", "jane@home.com"], config_path=cfg)
    assert out == {"name": "Jane Doe", "emails": ["jane@work.com", "jane@home.com"]}
    data = load_config(cfg)
    assert data["user"]["name"] == "Jane Doe"
    assert data["user"]["emails"] == ["jane@work.com", "jane@home.com"]


def test_set_identity_emails_must_be_list(tmp_path):
    with pytest.raises(ValueError, match="list"):
        ff.set_identity("X", "not-a-list", config_path=tmp_path / "c.yaml")


def test_set_identity_preserves_other_config(tmp_path):
    cfg = tmp_path / "c.yaml"
    ff.set_flag("generation.log_drafts", "false", config_path=cfg)
    ff.set_identity("Jane", ["j@x.com"], config_path=cfg)
    data = load_config(cfg)
    assert data["user"]["name"] == "Jane"
    assert data["generation"]["log_drafts"] is False  # untouched


def test_identity_endpoint(monkeypatch):
    captured = {}
    monkeypatch.setattr(ff, "set_identity", lambda name=None, emails=None: captured.update(n=name, e=emails) or {"name": name, "emails": emails})
    r = client.post("/api/config/identity", json={"name": "Jane", "emails": ["a@x.com"]})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "name": "Jane", "emails": ["a@x.com"]}
    assert captured == {"n": "Jane", "e": ["a@x.com"]}


# --- wizard page -----------------------------------------------------------


def test_welcome_page_has_all_steps_and_wiring():
    body = client.get("/welcome").text
    for n in range(7):  # 7-step comprehensive flow
        assert f'data-step="{n}"' in body
    assert "/api/config/identity" in body         # identity performed
    assert "ingestion.google_backend" in body     # backend performed via /api/config/set
    assert "/api/ingest" in body                   # ingestion run + status from the wizard
    assert "adapter_ready" in body                 # train-step readiness check
    assert "youos ingest" in body                 # terminal fallback still offered


def test_welcome_links_shared_assets():
    body = client.get("/welcome").text
    assert "/static/youos.css" in body and "/static/youos.js" in body


def test_feedback_empty_state_links_to_wizard():
    assert 'href="/welcome"' in client.get("/feedback").text
