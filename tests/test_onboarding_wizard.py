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
    # Display name is auto-derived as <First>OS during setup.
    assert out == {"name": "Jane Doe", "emails": ["jane@work.com", "jane@home.com"], "display_name": "JaneOS"}
    data = load_config(cfg)
    assert data["user"]["name"] == "Jane Doe"
    assert data["user"]["emails"] == ["jane@work.com", "jane@home.com"]
    assert data["user"]["display_name"] == "JaneOS"


def test_derive_os_name():
    assert ff.derive_os_name("Baher") == "BaherOS"
    assert ff.derive_os_name("Baher Al Hakim") == "BaherOS"   # first token only
    assert ff.derive_os_name("jane") == "JaneOS"               # capitalized
    assert ff.derive_os_name("McAvoy") == "McAvoyOS"           # internal casing kept
    assert ff.derive_os_name("") == "YouOS"                    # generic fallback
    assert ff.derive_os_name(None) == "YouOS"


def test_set_identity_explicit_display_name_wins(tmp_path):
    cfg = tmp_path / "c.yaml"
    out = ff.set_identity("Baher", ["b@x.com"], display_name="Custom Brand", config_path=cfg)
    assert out["display_name"] == "Custom Brand"


def test_set_identity_does_not_clobber_custom_display_name(tmp_path):
    cfg = tmp_path / "c.yaml"
    ff.set_identity("Baher", ["b@x.com"], display_name="Custom Brand", config_path=cfg)
    # A later name change must not overwrite the custom brand…
    out = ff.set_identity("Bob", ["b@x.com"], config_path=cfg)
    assert out["display_name"] == "Custom Brand"


def test_set_identity_updates_auto_display_name_on_rename(tmp_path):
    cfg = tmp_path / "c.yaml"
    ff.set_identity("Baher", ["b@x.com"], config_path=cfg)  # → BaherOS (auto)
    # …but an auto-derived one tracks the new name.
    out = ff.set_identity("Bob", ["b@x.com"], config_path=cfg)
    assert out["display_name"] == "BobOS"


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
    def fake_set_identity(name=None, emails=None, display_name=None):
        captured.update(n=name, e=emails)
        return {"name": name, "emails": emails}

    monkeypatch.setattr(ff, "set_identity", fake_set_identity)
    r = client.post("/api/config/identity", json={"name": "Jane", "emails": ["a@x.com"]})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "name": "Jane", "emails": ["a@x.com"]}
    assert captured == {"n": "Jane", "e": ["a@x.com"]}


# --- wizard page -----------------------------------------------------------


def test_welcome_page_has_all_steps_and_wiring():
    body = client.get("/welcome").text
    for n in range(8):  # comprehensive flow (incl. "Keep it running")
        assert f'data-step="{n}"' in body
    assert "/api/config/identity" in body         # identity performed
    assert "ingestion.google_backend" in body     # backend performed via /api/config/set
    assert "/api/ingest" in body                   # ingestion run + status from the wizard
    assert "adapter_ready" in body                 # train-step readiness check
    assert "youos ingest" in body                 # terminal fallback still offered


def test_welcome_auto_starts_and_reports_finetune():
    """Fine-tuning must be processed as part of onboarding, not silently skipped:
    it auto-starts on reaching the step, and the final step reports its status."""
    body = client.get("/welcome").text
    assert "autoStartFinetune" in body          # kicks off automatically
    assert "finetuneStepIdx" in body            # wired to the step's entry
    assert 'id="doneAdapter"' in body           # final step reports voice-model status
    assert "Re-run fine-tuning" in body         # manual re-trigger still available


def test_welcome_personalizes_to_user_os():
    """Setup brands the app as <First>OS (BaherOS) — the idea behind YouOS."""
    body = client.get("/welcome").text
    assert 'id="osNamePreview"' in body   # live preview element
    assert "deriveOsName" in body          # client-side derivation for the preview


def test_welcome_links_shared_assets():
    body = client.get("/welcome").text
    assert "/static/youos.css" in body and "/static/youos.js" in body


def test_welcome_has_plain_language_explainers():
    body = client.get("/welcome").text
    # one plain-language explainer callout per step (welcome..secure)
    assert body.count('class="explain"') >= 6
    assert "What YouOS does" in body  # jargon-free intro for non-technical users


def test_feedback_empty_state_links_to_wizard():
    assert 'href="/welcome"' in client.get("/feedback").text
