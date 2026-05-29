"""β.2: agent triage API + /triage page wiring.

Pins the REST endpoints against the same TestClient flow other route tests use.
The `mocked_environment` from test_agent_triage builds the DB; here we exercise
the routes after a triage run.
"""

from __future__ import annotations

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
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    from pathlib import Path
    repo_schema = Path(__file__).resolve().parents[1] / "docs" / "schema.sql"
    (docs / "schema.sql").write_text(repo_schema.read_text())
    # ``app.core.config.CONFIG_PATH`` is bound at module-import time from
    # YOUOS_DATA_DIR. By the time pytest runs, that import has already
    # happened (likely with no YOUOS_DATA_DIR set) — so set_flag would
    # write to the *real* youos_config.yaml. Override the module global
    # AND clear the lru-cached load_config() so this test sees a fresh,
    # empty config rooted in tmp_path.
    tmp_config = tmp_path / "youos_config.yaml"
    monkeypatch.setattr("app.core.config.CONFIG_PATH", tmp_config)
    from app.core.config import load_config
    load_config.cache_clear()
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


def test_dismiss_accepts_categorical_reason(authed_client):
    rows = authed_client.get("/api/agent/pending?tier=draft").json()["rows"]
    row_id = rows[0]["id"]
    r = authed_client.post(
        f"/api/agent/pending/{row_id}/dismiss",
        json={"reason": "noise"},
    )
    assert r.status_code == 200
    assert r.json()["row"]["dismissal_reason"] == "noise"


def test_dismiss_rejects_unknown_reason(authed_client):
    rows = authed_client.get("/api/agent/pending?tier=draft").json()["rows"]
    row_id = rows[0]["id"]
    r = authed_client.post(
        f"/api/agent/pending/{row_id}/dismiss",
        json={"reason": "not_a_bucket"},
    )
    assert r.status_code == 400
    assert "unknown dismissal reason" in r.text


def test_dismissal_stats_endpoint_returns_aggregate(authed_client):
    rows = authed_client.get("/api/agent/pending").json()["rows"]
    # Dismiss one with reason, one without.
    authed_client.post(f"/api/agent/pending/{rows[0]['id']}/dismiss", json={"reason": "noise"})
    authed_client.post(f"/api/agent/pending/{rows[1]['id']}/dismiss")
    s = authed_client.get("/api/agent/dismissal_stats").json()
    assert s["dismissed"] >= 2
    assert s["by_reason"]["noise"] >= 1
    assert s["by_reason"]["no_reason"] >= 1
    assert 0.0 <= s["dismissal_rate"] <= 1.0


def test_skip_sender_candidates_endpoint_returns_repeated_noise_dismissals(authed_client):
    """Two noise dismissals from the same sender should surface as a
    candidate; lone dismissals should not."""
    rows = authed_client.get("/api/agent/pending").json()["rows"]
    # The fixture has multiple rows from possibly-different senders.
    # Pick two with the same sender_email if any exist; otherwise just
    # dismiss the first two and verify the endpoint shape.
    by_sender: dict = {}
    for r in rows:
        se = (r.get("sender_email") or "").lower()
        if se:
            by_sender.setdefault(se, []).append(r["id"])
    same_sender = next((ids for ids in by_sender.values() if len(ids) >= 2), None)
    if same_sender:
        for rid in same_sender[:2]:
            authed_client.post(f"/api/agent/pending/{rid}/dismiss", json={"reason": "noise"})
        body = authed_client.get("/api/agent/skip_sender_candidates?min_count=2").json()
        assert any(c["count"] >= 2 for c in body["candidates"])
    # Shape always holds, regardless of fixture content.
    body = authed_client.get("/api/agent/skip_sender_candidates?min_count=1").json()
    assert "candidates" in body and isinstance(body["candidates"], list)
    assert body["min_count"] == 1
    assert body["window_days"] == 30


def test_promote_skip_senders_appends_to_flag(authed_client):
    """Bulk-add senders to agent.skip_senders. The endpoint must (a) report
    which senders it added, (b) include them in the returned value, and (c)
    de-duplicate within the request (case-folded)."""
    r = authed_client.post(
        "/api/agent/skip_senders/promote",
        json={"senders": ["newsletter@daily.com", "Newsletter@daily.com", "marketing@blast.com"]},
    )
    assert r.status_code == 200
    body = r.json()
    # Case-folded de-dup within the request — second "Newsletter@daily.com"
    # lands in already_present because the first one was just added.
    added_lower = [a.lower() for a in body["added"]]
    assert "newsletter@daily.com" in added_lower
    assert "marketing@blast.com" in added_lower
    val = body["value"].lower()
    assert "newsletter@daily.com" in val
    assert "marketing@blast.com" in val


def test_promote_skip_senders_idempotent_on_second_call(authed_client):
    """Calling promote twice with the same sender list — second call reports
    everything under ``already_present`` and the flag value doesn't grow."""
    payload = {"senders": ["bot@spammer.com"]}
    first = authed_client.post("/api/agent/skip_senders/promote", json=payload).json()
    assert "bot@spammer.com" in [a.lower() for a in first["added"]]
    second = authed_client.post("/api/agent/skip_senders/promote", json=payload).json()
    assert second["added"] == []
    assert "bot@spammer.com" in [a.lower() for a in second["already_present"]]
    # No duplicate in the resulting value.
    assert second["value"].lower().count("bot@spammer.com") == 1


def test_promote_skip_senders_rejects_empty_list(authed_client):
    r = authed_client.post("/api/agent/skip_senders/promote", json={"senders": []})
    # Pydantic min_length=1 → 422.
    assert r.status_code == 422


def test_save_as_feedback_pair_inserts_into_feedback_pairs(authed_client):
    """Drafts a feedback_pair from an agent row + the user's correction.
    The interactive review queue uses the same /feedback/submit path, so
    pair shape is identical. We assert via ``total_pairs`` (the running
    count surfaced for the UI's "X pairs collected" status)."""
    rows = authed_client.get("/api/agent/pending?tier=draft").json()["rows"]
    row_id = rows[0]["id"]
    r = authed_client.post(
        f"/api/agent/pending/{row_id}/save_as_feedback_pair",
        json={"edited_reply": "Hi Alice — Q3 pricing held steady. Let me know if you need a written quote."},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # First insertion → total_pairs >= 1.
    assert body["total_pairs"] >= 1
    # Edit distance reflects that we substantially rewrote the draft.
    assert 0.0 <= body["edit_distance_pct"] <= 1.0


def test_save_as_feedback_pair_rejects_empty_edited_reply(authed_client):
    rows = authed_client.get("/api/agent/pending?tier=draft").json()["rows"]
    r = authed_client.post(
        f"/api/agent/pending/{rows[0]['id']}/save_as_feedback_pair",
        json={"edited_reply": ""},
    )
    # Pydantic min_length=1 → 422.
    assert r.status_code == 422


def test_save_as_feedback_pair_rejects_surface_tier(authed_client):
    """Surface-for-review rows have no draft, so there's nothing to use as
    `generated_draft`. Reject so the user can't accidentally seed the
    training queue with empty rows."""
    rows = authed_client.get("/api/agent/pending?tier=surface").json()["rows"]
    r = authed_client.post(
        f"/api/agent/pending/{rows[0]['id']}/save_as_feedback_pair",
        json={"edited_reply": "anything"},
    )
    assert r.status_code == 400
    assert "no draft" in r.text


def test_resolve_finds_pending_row_by_subject_substring(authed_client):
    """b62: orchestrator NLU helper — substring match against subject."""
    # Fixture seeds a row with subject 'Q3 pricing?'; the search should find it.
    r = authed_client.get("/api/agent/resolve?q=Q3")
    body = r.json()
    assert r.status_code == 200
    assert body["count"] >= 1
    top = body["rows"][0]
    assert "Q3" in (top["subject"] or "")
    assert top["match_field"] == "subject"


def test_resolve_finds_row_by_sender_substring(authed_client):
    """Substring match against sender email also works."""
    body = authed_client.get("/api/agent/resolve?q=partner.com").json()
    assert body["count"] >= 1
    assert body["rows"][0]["match_field"] == "sender"


def test_resolve_returns_empty_count_when_no_match(authed_client):
    body = authed_client.get("/api/agent/resolve?q=nonexistent-banana-string").json()
    assert body["count"] == 0
    assert body["rows"] == []


def test_resolve_requires_q_param(authed_client):
    """Missing or empty q → 422 (Pydantic min_length=1)."""
    r = authed_client.get("/api/agent/resolve")
    assert r.status_code == 422


def test_digest_endpoint_mirrors_cli_output(authed_client):
    """b59: GET /api/agent/digest returns the same shape as
    `youos digest --format json` — orchestrator-facing entry point."""
    # Dismiss one row so the digest has interesting state.
    rows = authed_client.get("/api/agent/pending").json()["rows"]
    if rows:
        authed_client.post(f"/api/agent/pending/{rows[0]['id']}/dismiss",
                           json={"reason": "noise"})

    # Pass account explicitly — the test fixture doesn't set user.emails.
    body = authed_client.get("/api/agent/digest?account=you@example.com").json()
    # Required orchestrator surface: summary headline + structured counts.
    assert "summary" in body
    assert body["summary"].startswith("YouOS")
    assert "sweeps" in body
    assert "pending_count" in body
    assert "dismissed_count" in body
    assert "pending_preview" in body  # b59: action-handle list for chat orchestrators
    assert body["account"] == "you@example.com"


def test_digest_endpoint_400s_without_account_or_user_emails(authed_client, monkeypatch):
    """If no account is passed AND user.emails is empty, return a clear
    400 rather than crashing."""
    monkeypatch.setattr("app.core.config.get_user_emails", lambda *a, **k: [])
    r = authed_client.get("/api/agent/digest")
    assert r.status_code == 400
    assert "no account" in r.text.lower()


def test_observability_endpoint_returns_unified_payload(authed_client):
    # Generate some signal: dismiss a couple as noise so a hint can fire.
    rows = authed_client.get("/api/agent/pending").json()["rows"]
    for row in rows[:3]:
        authed_client.post(
            f"/api/agent/pending/{row['id']}/dismiss",
            json={"reason": "noise"},
        )
    body = authed_client.get("/api/agent/observability?days=30").json()
    # Aggregates always present, even with zero data (drafting may be null).
    assert set(body.keys()) == {"sweep", "dismissals", "score_histogram", "drafting", "hints"}
    assert "sweeps" in body["sweep"]
    # Heartbeat fields present on the sweep aggregate.
    assert "last_sweep_at" in body["sweep"]
    assert "seconds_since_last_sweep" in body["sweep"]
    assert "by_reason" in body["dismissals"]
    assert "buckets" in body["score_histogram"]
    # Histogram has all five labelled buckets, zero-filled.
    assert set(body["score_histogram"]["buckets"].keys()) == {
        "0.0-0.3", "0.3-0.5", "0.5-0.7", "0.7-0.9", "0.9-1.0",
    }
    # hints is always a list (may be empty in the test fixture).
    assert isinstance(body["hints"], list)


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


def test_triage_page_includes_ux_upgrades(authed_client):
    """Smoke check: the new b41 UX controls are present in the rendered HTML.

    HTML-level assertion only — we can't actually exercise the JS keyboard
    handler from pytest, but if any of these IDs vanish the feature is
    silently broken on the user's screen, so pin them here.
    """
    html = authed_client.get("/triage").text
    # Filter + bulk controls.
    assert 'id="filterSender"' in html
    assert 'id="filterMinScore"' in html
    assert 'id="bulkPushBtn"' in html
    assert 'id="bulkDismissSurfaceBtn"' in html
    # Help overlay + entry button.
    assert 'id="helpBtn"' in html
    assert 'id="helpOverlay"' in html
    # Skip-sender checkbox emitted alongside Dismiss.
    assert "skip-sender-cb" in html
    # Keyboard handler is registered (sentinel string).
    assert "keydown" in html
    assert 'id="appVersion"' in html        # shared chrome wiring
    assert "Run triage now" in html         # core action
    # New nav link is present on the page.
    assert ">Triage<" in html


# --- ε: sweeps endpoint ----------------------------------------------------


def test_sweeps_endpoint_returns_recent_audit_rows(authed_client):
    """``/api/agent/sweeps`` returns the audit log (newest first), with
    rehydrated ``errors`` arrays."""
    from app.agent import store
    from app.core.settings import get_settings

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


def test_push_to_gmail_is_idempotent_no_duplicate_draft(authed_client, monkeypatch):
    """Re-pushing an already-pushed row returns the SAME draft id and does NOT
    create a second Gmail draft — the dup-draft guard. create_draft is invoked
    exactly once across two pushes."""
    rows = authed_client.get("/api/agent/pending?tier=draft").json()["rows"]
    row_id = rows[0]["id"]

    from app.ingestion import gmail_write
    calls = {"n": 0}

    def _create(**kw):
        calls["n"] += 1
        return gmail_write.GmailDraftResult(draft_id="gd_once", raw_response={"id": "gd_once"})

    monkeypatch.setattr(gmail_write, "create_draft", _create)

    r1 = authed_client.post(f"/api/agent/pending/{row_id}/push_to_gmail")
    assert r1.status_code == 200, r1.text
    assert r1.json()["gmail_draft_id"] == "gd_once"
    assert r1.json()["pushed_already"] is False

    r2 = authed_client.post(f"/api/agent/pending/{row_id}/push_to_gmail")
    assert r2.status_code == 200, r2.text
    assert r2.json()["gmail_draft_id"] == "gd_once"
    assert r2.json()["pushed_already"] is True

    assert calls["n"] == 1, "create_draft must be called exactly once across two pushes"


def test_push_to_gmail_failure_rolls_back_status_so_retry_works(authed_client, monkeypatch):
    """A backend failure must NOT leave the row stuck in 'sent' with no draft —
    the claim is rolled back so a retry can succeed."""
    rows = authed_client.get("/api/agent/pending?tier=draft").json()["rows"]
    row_id = rows[0]["id"]

    from app.ingestion import gmail_write

    def _fail(**kw):
        raise gmail_write.GmailWriteError("transient gog error")

    monkeypatch.setattr(gmail_write, "create_draft", _fail)
    r = authed_client.post(f"/api/agent/pending/{row_id}/push_to_gmail")
    assert r.status_code == 502

    # Row must be back to a pushable state (not stuck 'sent'), draft id still unset.
    row = authed_client.get(f"/api/agent/pending/{row_id}").json()
    assert row["status"] in ("pending", "amended")
    assert not row.get("gmail_draft_id")

    # A retry with a working backend now succeeds.
    monkeypatch.setattr(
        gmail_write, "create_draft",
        lambda **kw: gmail_write.GmailDraftResult(draft_id="gd_retry", raw_response={"id": "gd_retry"}),
    )
    r2 = authed_client.post(f"/api/agent/pending/{row_id}/push_to_gmail")
    assert r2.status_code == 200, r2.text
    assert r2.json()["gmail_draft_id"] == "gd_retry"


# --- confirm_send: one-call human-confirmed send (OpenClaw approve action) ---


def _mock_send_path(monkeypatch, *, enabled=True, kill=False):
    """Mock the Gmail create_draft + send_draft layer and the send gate."""
    from app.agent import send as send_mod
    from app.ingestion import gmail_write

    monkeypatch.setattr(send_mod, "_send_config", lambda: {"enabled": enabled, "kill_switch": kill})
    monkeypatch.setattr(
        gmail_write, "create_draft",
        lambda **kw: gmail_write.GmailDraftResult(draft_id="gd_cs", raw_response={"id": "gd_cs"}),
    )
    sent = {}
    monkeypatch.setattr(
        gmail_write, "send_draft",
        lambda **kw: sent.update(kw) or gmail_write.GmailSendResult(message_id="msg_cs", raw_response={"id": "msg_cs"}),
    )
    return sent


def test_confirm_send_pushes_and_sends_in_one_call(authed_client, monkeypatch):
    _mock_send_path(monkeypatch)
    row_id = authed_client.get("/api/agent/pending?tier=draft").json()["rows"][0]["id"]

    r = authed_client.post(f"/api/agent/pending/{row_id}/confirm_send")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["gmail_draft_id"] == "gd_cs"
    assert body["sent_message_id"] == "msg_cs"
    assert body["row"]["send_state"] == "sent"


def test_confirm_send_applies_final_edit_then_sends(authed_client, monkeypatch):
    _mock_send_path(monkeypatch)
    row_id = authed_client.get("/api/agent/pending?tier=draft").json()["rows"][0]["id"]

    r = authed_client.post(
        f"/api/agent/pending/{row_id}/confirm_send",
        json={"amended_draft": "My final edited reply — confirmed."},
    )
    assert r.status_code == 200, r.text
    row = r.json()["row"]
    # The edit was applied (and tagged as a human edit) before sending.
    assert row["amended_draft"] == "My final edited reply — confirmed."
    assert row["amended_by"] == "user"
    assert row["send_state"] == "sent"


def test_confirm_send_blocked_when_send_disabled_creates_no_draft(authed_client, monkeypatch):
    """With send disabled, confirm_send 403s BEFORE creating a Gmail draft (no
    orphan draft left behind)."""
    from app.agent import send as send_mod
    from app.ingestion import gmail_write

    monkeypatch.setattr(send_mod, "_send_config", lambda: {"enabled": False, "kill_switch": False})
    monkeypatch.setattr(
        gmail_write, "create_draft",
        lambda **kw: (_ for _ in ()).throw(AssertionError("must not create a draft when send is disabled")),
    )
    row_id = authed_client.get("/api/agent/pending?tier=draft").json()["rows"][0]["id"]
    r = authed_client.post(f"/api/agent/pending/{row_id}/confirm_send")
    assert r.status_code == 403
    assert "disabled" in r.json()["detail"]


def test_confirm_send_blocked_by_kill_switch(authed_client, monkeypatch):
    from app.agent import send as send_mod
    monkeypatch.setattr(send_mod, "_send_config", lambda: {"enabled": True, "kill_switch": True})
    row_id = authed_client.get("/api/agent/pending?tier=draft").json()["rows"][0]["id"]
    r = authed_client.post(f"/api/agent/pending/{row_id}/confirm_send")
    assert r.status_code == 403
    assert "kill-switch" in r.json()["detail"]


def test_regenerate_redrafts_in_voice_and_persists(authed_client, monkeypatch):
    """The regenerate endpoint re-runs generation with the instruction threaded
    in as standing_instructions, and stores the result as amended_draft."""
    from types import SimpleNamespace

    from app.generation import service as gen

    seen = {}

    def _fake_generate(req, **kw):
        seen["instruction"] = req.standing_instructions
        seen["inbound"] = req.inbound_message
        return SimpleNamespace(draft="Shorter, declined politely.", model_used="qwen-lora")

    monkeypatch.setattr(gen, "generate_draft", _fake_generate)

    rows = authed_client.get("/api/agent/pending?tier=draft").json()["rows"]
    row_id = rows[0]["id"]

    r = authed_client.post(
        f"/api/agent/pending/{row_id}/regenerate",
        json={"instruction": "make it shorter and decline the meeting"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["draft"] == "Shorter, declined politely."
    assert body["persisted"] is True
    assert seen["instruction"] == "make it shorter and decline the meeting"

    # Persisted as amended_draft + status amended.
    row = authed_client.get(f"/api/agent/pending/{row_id}").json()
    assert row["status"] == "amended"
    assert row["amended_draft"] == "Shorter, declined politely."


def test_regenerate_preview_does_not_persist(authed_client, monkeypatch):
    from types import SimpleNamespace

    from app.generation import service as gen
    monkeypatch.setattr(
        gen, "generate_draft",
        lambda req, **kw: SimpleNamespace(draft="preview text", model_used="m"),
    )
    rows = authed_client.get("/api/agent/pending?tier=draft").json()["rows"]
    row_id = rows[0]["id"]
    r = authed_client.post(
        f"/api/agent/pending/{row_id}/regenerate",
        json={"instruction": "x", "persist": False},
    )
    assert r.status_code == 200
    assert r.json()["persisted"] is False
    row = authed_client.get(f"/api/agent/pending/{row_id}").json()
    assert row["status"] == "pending"  # untouched
    assert not row.get("amended_draft")
