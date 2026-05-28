"""β.2: agent triage API + /triage page wiring.

Pins the REST endpoints against the same TestClient flow other route tests use.
The `mocked_environment` from test_agent_triage builds the DB; here we exercise
the routes after a triage run.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def authed_client(monkeypatch, tmp_path):
    """Stand up the app pointed at a fresh tmp instance, with a couple of
    pre-loaded ``agent_pending_drafts`` rows (one draft tier, one surface)
    so the API tests have something to act on."""
    # Set up an empty instance under tmp_path.
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("YOUOS_DATABASE_URL", f"sqlite:///{tmp_path}/var/youos.db")
    (tmp_path / "var").mkdir(exist_ok=True)
    (tmp_path / "configs").mkdir(exist_ok=True)
    docs = tmp_path / "docs"; docs.mkdir(exist_ok=True)
    from pathlib import Path
    repo_schema = Path(__file__).resolve().parents[1] / "docs" / "schema.sql"
    (docs / "schema.sql").write_text(repo_schema.read_text())
    from app.core.settings import get_settings
    get_settings.cache_clear()
    from app.db.bootstrap import bootstrap_database
    bootstrap_database()

    # Pre-load fixtures.
    from app.agent import store
    db_url = f"sqlite:///{tmp_path}/var/youos.db"
    store.upsert_pending(
        db_url,
        message_id="m-draft", thread_id="t-1", account="you@example.com",
        sender="Alice <alice@partner.com>", sender_email="alice@partner.com",
        subject="Q3 pricing?", body="Could you confirm the Q3 pricing?",
        received_at="2026-05-28T10:00:00Z",
        needs_reply_score=0.85,
        reasons=["ends with a question", "imperative verb present"],
        cold_outreach=False, tier="draft",
        draft="Confirmed — pricing unchanged.", draft_model="qwen2.5-1.5b-lora",
        draft_repairs=["stripped_trailing_signature"],
        standing_instructions_snapshot=None,
    )
    store.upsert_pending(
        db_url,
        message_id="m-surface", thread_id="t-2", account="you@example.com",
        sender="System <noreply@vendor.com>", sender_email="noreply@vendor.com",
        subject="Possibly-something", body="Borderline content.",
        received_at="2026-05-28T11:00:00Z",
        needs_reply_score=0.45,
        reasons=["noreply sender (transactional or marketing)"],
        cold_outreach=False, tier="surface",
        draft=None, draft_model=None, draft_repairs=[],
        standing_instructions_snapshot=None,
    )

    from app.main import app
    # ``app`` is module-level; ``state.settings`` was bound to whatever env
    # was first seen by the import. Force it to point at *this* test's DB
    # so each test runs in isolation.
    app.state.settings = get_settings()
    yield TestClient(app)
    get_settings.cache_clear()


def test_list_pending_returns_both_tiers(authed_client):
    r = authed_client.get("/api/agent/pending")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    tiers = {row["tier"] for row in body["rows"]}
    assert tiers == {"draft", "surface"}


def test_list_pending_filters_by_tier(authed_client):
    r = authed_client.get("/api/agent/pending?tier=draft")
    body = r.json()
    assert body["count"] == 1
    assert body["rows"][0]["tier"] == "draft"
    assert body["rows"][0]["subject"] == "Q3 pricing?"


def test_amend_updates_amended_draft_and_status(authed_client):
    # Find the draft-tier row.
    rows = authed_client.get("/api/agent/pending?tier=draft").json()["rows"]
    row_id = rows[0]["id"]
    r = authed_client.post(
        f"/api/agent/pending/{row_id}/amend",
        json={"amended_draft": "Confirmed — Q3 pricing held."},
    )
    assert r.status_code == 200
    after = r.json()["row"]
    assert after["status"] == "amended"
    assert after["amended_draft"] == "Confirmed — Q3 pricing held."


def test_dismiss_marks_dismissed_and_drops_from_default_list(authed_client):
    rows = authed_client.get("/api/agent/pending").json()["rows"]
    row_id = next(r["id"] for r in rows if r["tier"] == "surface")
    r = authed_client.post(f"/api/agent/pending/{row_id}/dismiss")
    assert r.status_code == 200
    after_listing = authed_client.get("/api/agent/pending").json()["rows"]
    assert all(row["id"] != row_id for row in after_listing), "dismissed row should leave the default listing"


def test_mark_sent_records_timestamp(authed_client):
    rows = authed_client.get("/api/agent/pending?tier=draft").json()["rows"]
    row_id = rows[0]["id"]
    r = authed_client.post(f"/api/agent/pending/{row_id}/mark_sent")
    assert r.status_code == 200
    sent = r.json()["row"]
    assert sent["status"] == "sent"
    assert sent["sent_at"] is not None


def test_amend_on_missing_row_returns_404(authed_client):
    r = authed_client.post("/api/agent/pending/99999/amend", json={"amended_draft": "x"})
    assert r.status_code == 404


def test_triage_page_renders_with_nav_and_assets(authed_client):
    r = authed_client.get("/triage")
    assert r.status_code == 200
    html = r.text
    assert "Agent triage" in html
    assert "/static/youos.css" in html
    assert 'id="appVersion"' in html        # shared chrome wiring
    assert "Run triage now" in html         # core action
    # New nav link is present on the page.
    assert ">Triage<" in html


# --- ε: sweeps endpoint ----------------------------------------------------


def test_sweeps_endpoint_returns_recent_audit_rows(authed_client):
    """``/api/agent/sweeps`` returns the audit log (newest first), with
    rehydrated ``errors`` arrays."""
    from app.core.settings import get_settings
    from app.agent import store

    db_url = get_settings().database_url
    store.log_sweep(
        db_url, account="you@example.com", trigger="manual",
        window="3d", threshold=0.6,
        fetched=4, kept=1, surfaced=1, persisted=1, errors=[],
        standing_instructions_snapshot=None,
        started_at="2026-05-28T10:00:00Z", finished_at="2026-05-28T10:00:02Z",
        duration_ms=2000,
    )

    r = authed_client.get("/api/agent/sweeps?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    assert body["sweeps"][0]["trigger"] == "manual"
    assert body["sweeps"][0]["errors"] == []


# --- Phase 2.1: push_to_gmail endpoint -------------------------------------


def test_push_to_gmail_success_stores_draft_id_and_marks_sent(authed_client, monkeypatch):
    """Happy path: gmail_write returns a draft id; the row gets gmail_draft_id
    set and status flipped to ``sent``."""
    from app.agent import store
    from app.core.settings import get_settings
    db_url = get_settings().database_url

    rows = authed_client.get("/api/agent/pending?tier=draft").json()["rows"]
    row_id = rows[0]["id"]

    # Mock the gmail-write layer so we don't touch real gog.
    from app.ingestion import gmail_write
    monkeypatch.setattr(
        gmail_write, "create_draft",
        lambda **kw: gmail_write.GmailDraftResult(
            draft_id="gd_999",
            raw_response={"id": "gd_999", "message": {"id": "m_456"}},
        ),
    )

    r = authed_client.post(f"/api/agent/pending/{row_id}/push_to_gmail")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["gmail_draft_id"] == "gd_999"
    row = body["row"]
    assert row["status"] == "sent"
    assert row["sent_at"] is not None
    assert row["gmail_draft_id"] == "gd_999"


def test_push_to_gmail_rejects_surface_tier_row(authed_client):
    """tier='surface' rows have no draft → 400, not pushed."""
    rows = authed_client.get("/api/agent/pending?tier=surface").json()["rows"]
    row_id = rows[0]["id"]
    r = authed_client.post(f"/api/agent/pending/{row_id}/push_to_gmail")
    assert r.status_code == 400
    assert "no draft to push" in r.json()["detail"].lower()


def test_push_to_gmail_propagates_not_implemented_as_501(authed_client, monkeypatch):
    """gws/native backends raise NotImplementedError → HTTP 501."""
    rows = authed_client.get("/api/agent/pending?tier=draft").json()["rows"]
    row_id = rows[0]["id"]

    from app.ingestion import gmail_write
    def _raise(**kw):
        raise NotImplementedError("gws backend doesn't yet support draft creation")
    monkeypatch.setattr(gmail_write, "create_draft", _raise)

    r = authed_client.post(f"/api/agent/pending/{row_id}/push_to_gmail")
    assert r.status_code == 501
    assert "gws" in r.json()["detail"]


def test_push_to_gmail_propagates_gmail_write_error_as_502(authed_client, monkeypatch):
    """A backend-level failure (auth, network, etc.) → HTTP 502 with the
    underlying error message so the UI can surface it."""
    rows = authed_client.get("/api/agent/pending?tier=draft").json()["rows"]
    row_id = rows[0]["id"]

    from app.ingestion import gmail_write
    def _raise(**kw):
        raise gmail_write.GmailWriteError("gog returned exit 1: scope not granted")
    monkeypatch.setattr(gmail_write, "create_draft", _raise)

    r = authed_client.post(f"/api/agent/pending/{row_id}/push_to_gmail")
    assert r.status_code == 502
    assert "scope not granted" in r.json()["detail"]
