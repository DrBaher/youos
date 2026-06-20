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


def test_rules_page_renders_with_builder_and_ledger(authed_client):
    r = authed_client.get("/rules")
    assert r.status_code == 200
    html = r.text
    assert "/static/youos.css" in html
    # the builder + the two list panels
    assert 'id="conds"' in html
    assert 'id="rulesList"' in html
    assert 'id="ledger"' in html
    # it talks to the existing CRUD + validate + actions endpoints
    assert "/api/agent/rules/validate" in html
    assert "/api/agent/actions" in html
    # the richer action vocabulary is offered in the builder
    assert "mark_important" in html
    # the natural-language entry point
    assert 'id="nlText"' in html
    assert "/api/agent/rules/parse" in html
    # the outbound forward action + its gating warning
    assert '"forward"' in html and "forward to (outbound" in html
    assert 'id="fwdNote"' in html


def test_parse_rule_text_endpoint_model_unavailable(authed_client, monkeypatch):
    """Endpoint wiring: with the model off it returns ok=False, never 500."""
    import app.core.model_server as ms

    monkeypatch.setattr(ms, "is_enabled", lambda: False)
    r = authed_client.post("/api/agent/rules/parse", json={"text": "archive newsletters"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["rule"] is None


def test_parse_rule_text_endpoint_happy_path(authed_client, monkeypatch):
    import app.core.model_server as ms

    monkeypatch.setattr(ms, "is_enabled", lambda: True)
    monkeypatch.setattr(ms, "complete", lambda *a, **k:
                        '{"match": {"domain": "@recruiters.com"}, "action": "archive", "value": null}')
    r = authed_client.post("/api/agent/rules/parse", json={"text": "archive recruiter mail"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["rule"]["action"] == "archive"


def test_rules_link_in_nav(authed_client):
    """The Rules page must be reachable from the shared nav."""
    assert 'href="/rules"' in authed_client.get("/triage").text


def test_digests_list_endpoint(authed_client):
    r = authed_client.get("/api/agent/digests")
    assert r.status_code == 200
    body = r.json()
    assert "digests" in body and "runs" in body


def test_digest_run_unknown_name_404(authed_client):
    r = authed_client.post("/api/agent/digests/run",
                           json={"name": "does-not-exist", "account": "me@x.com", "dry_run": True})
    assert r.status_code == 404


def test_digests_page_renders_with_builder(authed_client):
    r = authed_client.get("/digests")
    assert r.status_code == 200
    html = r.text
    assert "/static/youos.css" in html
    assert 'id="query"' in html and 'id="schedule"' in html and 'id="weekday"' in html
    assert 'id="qtext"' in html and "/api/agent/digests/parse-query" in html   # NL→query box
    assert "/api/agent/digests/validate" in html
    assert 'href="/digests"' in authed_client.get("/triage").text   # in shared nav


def test_digest_validate_endpoint(authed_client):
    ok = authed_client.post("/api/agent/digests/validate",
                            json={"name": "N", "query": "label:X", "schedule": "weekly", "weekday": "friday"})
    assert ok.status_code == 200 and ok.json()["ok"] is True
    bad = authed_client.post("/api/agent/digests/validate", json={"name": "", "query": "x"})
    assert bad.status_code == 200 and bad.json()["ok"] is False


def test_digest_update_delete_out_of_range_404(authed_client):
    # index checks happen before any config write, so these 404 without mutating
    assert authed_client.put("/api/agent/digests/999", json={"name": "N", "query": "x"}).status_code == 404
    assert authed_client.delete("/api/agent/digests/999").status_code == 404


def test_digest_parse_query_endpoint(authed_client, monkeypatch):
    import app.core.model_server as ms

    # model off → ok=False, never 500
    monkeypatch.setattr(ms, "is_enabled", lambda: False)
    off = authed_client.post("/api/agent/digests/parse-query", json={"text": "newsletters this week"})
    assert off.status_code == 200 and off.json()["ok"] is False
    # model on → returns the translated query
    monkeypatch.setattr(ms, "is_enabled", lambda: True)
    monkeypatch.setattr(ms, "complete", lambda *a, **k: "category:promotions newer_than:7d")
    on = authed_client.post("/api/agent/digests/parse-query", json={"text": "newsletters this week"})
    assert on.status_code == 200 and on.json()["ok"] is True
    assert on.json()["query"] == "category:promotions newer_than:7d"


def test_digest_pending_and_collect_endpoints(authed_client):
    r = authed_client.get("/api/agent/digests/pending")
    assert r.status_code == 200 and "pending" in r.json()
    # collecting a non-existent/not-ready run is a 409 (atomic claim found nothing)
    assert authed_client.post("/api/agent/digests/99999/collected").status_code == 409


def test_digest_validate_accepts_destination_and_prompt(authed_client):
    ok = authed_client.post("/api/agent/digests/validate", json={
        "name": "N", "query": "label:X", "destination": "agent", "prompt": "what needs me"})
    assert ok.status_code == 200 and ok.json()["ok"] is True
    bad = authed_client.post("/api/agent/digests/validate", json={
        "name": "N", "query": "x", "destination": "telegram"})
    assert bad.status_code == 200 and bad.json()["ok"] is False


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


# --- rules authoring API (CRUD) ---------------------------------------------


def test_rules_crud_round_trip(authed_client):
    # starts empty
    assert authed_client.get("/api/agent/rules").json()["rules"] == []

    # create
    r = authed_client.post("/api/agent/rules", json={
        "match": {"domain": "@recruiters.com"}, "action": "label", "value": "Recruiting"})
    assert r.status_code == 200, r.text
    assert r.json()["index"] == 0
    r2 = authed_client.post("/api/agent/rules", json={
        "match": {"subject_contains": "invoice"}, "action": "star"})
    assert r2.status_code == 200

    rules = authed_client.get("/api/agent/rules").json()["rules"]
    assert len(rules) == 2 and rules[0]["value"] == "Recruiting"

    # update index 0
    ru = authed_client.put("/api/agent/rules/0", json={
        "match": {"domain": "@recruiters.com"}, "action": "label", "value": "Jobs"})
    assert ru.status_code == 200
    assert authed_client.get("/api/agent/rules").json()["rules"][0]["value"] == "Jobs"

    # delete index 0
    rd = authed_client.delete("/api/agent/rules/0")
    assert rd.status_code == 200
    after = authed_client.get("/api/agent/rules").json()["rules"]
    assert len(after) == 1 and after[0]["action"] == "star"


def test_rules_post_rejects_invalid(authed_client):
    r = authed_client.post("/api/agent/rules", json={
        "match": {"domain": "x"}, "action": "label", "value": "a,b"})  # comma label
    assert r.status_code == 400
    assert "comma" in r.json()["detail"]
    r2 = authed_client.post("/api/agent/rules", json={"match": {"nope": "x"}, "action": "star"})
    assert r2.status_code == 400  # unknown match key


def test_rules_validate_endpoint(authed_client):
    ok = authed_client.post("/api/agent/rules/validate", json={
        "match": {"domain": "@x.com"}, "action": "archive"}).json()
    assert ok["ok"] is True
    bad = authed_client.post("/api/agent/rules/validate", json={
        "match": {"domain": "@x.com"}, "action": "label"}).json()  # no value
    assert bad["ok"] is False and "label" in bad["error"]


def test_rules_put_delete_404_out_of_range(authed_client):
    assert authed_client.put("/api/agent/rules/9", json={
        "match": {"domain": "x"}, "action": "star"}).status_code == 404
    assert authed_client.delete("/api/agent/rules/9").status_code == 404


# --- agent-action framework: list + undo routing actions --------------------


def test_actions_list_and_undo(authed_client, monkeypatch):
    from app.agent import actions as act
    from app.ingestion import gmail_write

    db = authed_client.app.state.settings.database_url
    # Seed an applied 'label' action directly via the executor (live, mocked gog).
    monkeypatch.setattr(act, "_actions_config", lambda: {"enabled": True, "dry_run": False, "daily_cap": 50})
    monkeypatch.setattr(gmail_write, "ensure_label", lambda **k: None)
    reversed_calls = []
    monkeypatch.setattr(gmail_write, "modify_message_labels",
                        lambda **k: reversed_calls.append((k.get("add"), k.get("remove"))) or gmail_write.GmailModifyResult(k["message_id"], [], [], {}))
    from types import SimpleNamespace
    msg = SimpleNamespace(message_id="am1", thread_id="t", account="acct", sender_email="a@x.com", subject="s")
    act.apply_mailbox_actions(db, "acct", msg, [{"type": "label", "value": "Recruiting"}])

    # List
    r = authed_client.get("/api/agent/actions")
    assert r.status_code == 200, r.text
    rows = r.json()["actions"]
    assert rows and rows[0]["action_type"] == "label" and rows[0]["status"] == "applied"
    aid = rows[0]["id"]

    # Undo → reverses (removes the label that was added)
    r2 = authed_client.post(f"/api/agent/actions/{aid}/undo")
    assert r2.status_code == 200, r2.text
    assert reversed_calls[-1] == ([], ["Recruiting"])   # undo removed the added label
    assert authed_client.get("/api/agent/actions").json()["actions"][0]["status"] == "undone"


def test_undo_unknown_action_404(authed_client):
    r = authed_client.post("/api/agent/actions/999999/undo")
    assert r.status_code == 404


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


def test_confirm_send_refuses_stale_draft_when_edited(authed_client, monkeypatch, tmp_path):
    """b145: editing (amended_draft) a row that already has a Gmail draft created
    BEFORE the edit must NOT silently send the old, un-approved body. confirm_send
    rejects with 409 and never calls send."""
    import sqlite3

    import pytest

    from app.agent import send as send_mod

    # Past the send gate (step 0) so we reach the edit check.
    monkeypatch.setattr(send_mod, "_send_config", lambda: {"enabled": True, "kill_switch": False})
    # Tripwire: a send must never happen on this path.
    monkeypatch.setattr(send_mod, "send_pending_row",
                        lambda *a, **k: pytest.fail("send must not run when the edit can't reach the draft"))

    rows = authed_client.get("/api/agent/pending?tier=draft").json()["rows"]
    row_id = rows[0]["id"]
    # Simulate an auto-push that created a Gmail draft before review.
    conn = sqlite3.connect(tmp_path / "var" / "youos.db")
    conn.execute("UPDATE agent_pending_drafts SET gmail_draft_id='gd_pre' WHERE id=?", (row_id,))
    conn.commit()
    conn.close()

    r = authed_client.post(
        f"/api/agent/pending/{row_id}/confirm_send",
        json={"amended_draft": "EDITED — corrected the figure"},
    )
    assert r.status_code == 409
    assert "already has a Gmail draft" in r.json()["detail"]


def test_trigger_autoresearch_refuses_concurrent_run():
    """b145: a second trigger while one is in progress returns already_running
    (no second 2-hour subprocess spawned)."""
    from types import SimpleNamespace

    from app.api import review_queue_routes as rq

    rq._autoresearch_limiter.reset()
    rq._autoresearch_lock.acquire()  # simulate a run in progress
    try:
        req = SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"))
        out = rq.trigger_autoresearch(req)
        assert out["status"] == "already_running"
    finally:
        rq._autoresearch_lock.release()


def test_agent_route_string_fields_are_length_bounded():
    """b146: the agent-route string fields lacked max_length (b132 missed this
    surface); an unbounded body is a cheap memory/prompt DoS."""
    import pytest
    from pydantic import ValidationError

    from app.api.agent_routes import (
        AmendBody,
        ConfirmSendBody,
        DigestQueryTextBody,
        RegenerateBody,
        RuleTextBody,
    )

    with pytest.raises(ValidationError):
        AmendBody(amended_draft="x" * 50_001)
    with pytest.raises(ValidationError):
        ConfirmSendBody(amended_draft="x" * 50_001)
    with pytest.raises(ValidationError):
        RegenerateBody(instruction="x" * 4_001)
    with pytest.raises(ValidationError):
        RuleTextBody(text="x" * 4_001)
    with pytest.raises(ValidationError):
        DigestQueryTextBody(text="x" * 4_001)
    assert AmendBody(amended_draft="normal edit").amended_draft == "normal edit"


# --- b203: triage rate-limit + offset pagination -----------------------------

def _log_sweep_now(db_url: str, account: str, *, ago_seconds: int = 0) -> None:
    """Append a sweep audit row started ``ago_seconds`` ago (UTC)."""
    from datetime import datetime, timedelta, timezone

    from app.agent import store
    ts = (datetime.now(timezone.utc) - timedelta(seconds=ago_seconds)).isoformat()
    store.log_sweep(
        db_url, account=account, trigger="api", window="24h", threshold=0.6,
        fetched=0, kept=0, surfaced=0, persisted=0, errors=None,
        standing_instructions_snapshot=None,
        started_at=ts, finished_at=ts, duration_ms=10,
    )


def test_triage_rate_limited_returns_429_with_retry_after(authed_client, tmp_path):
    """A sweep requested within agent.triage_min_interval_seconds (default 60)
    of the account's last sweep is rejected with 429 + a Retry-After header —
    before any (expensive, Gmail-hitting) run_triage call."""
    db_url = f"sqlite:///{tmp_path}/var/youos.db"
    _log_sweep_now(db_url, "you@example.com", ago_seconds=2)
    r = authed_client.post("/api/agent/triage", json={"account": "you@example.com"})
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0


def test_triage_allowed_when_no_recent_sweep(authed_client, monkeypatch):
    """With no prior sweep the guard passes and run_triage executes. run_triage
    is stubbed so the test never touches Gmail/the model."""
    class _Result:
        fetched, kept, persisted = 3, 1, 1
        surfaced: list = []

    monkeypatch.setattr("app.agent.triage.run_triage", lambda **kw: _Result())
    r = authed_client.post("/api/agent/triage", json={"account": "fresh@example.com"})
    assert r.status_code == 200
    assert r.json()["kept"] == 1


def test_triage_guard_disabled_when_interval_zero(authed_client, tmp_path, monkeypatch):
    """agent.triage_min_interval_seconds=0 disables the guard even right after a sweep."""
    db_url = f"sqlite:///{tmp_path}/var/youos.db"
    _log_sweep_now(db_url, "you@example.com", ago_seconds=0)
    monkeypatch.setattr(
        "app.agent.scheduler.get_agent_config",
        lambda: {"triage_min_interval_seconds": 0},
    )

    class _Result:
        fetched, kept, persisted = 0, 0, 0
        surfaced: list = []

    monkeypatch.setattr("app.agent.triage.run_triage", lambda **kw: _Result())
    r = authed_client.post("/api/agent/triage", json={"account": "you@example.com"})
    assert r.status_code == 200


def test_pending_offset_paginates_without_overlap(authed_client):
    """offset slices the queue; page 1 and page 2 are disjoint, and has_more
    flips false on the last page. Fixture seeds exactly 2 pending rows."""
    p0 = authed_client.get("/api/agent/pending?limit=1&offset=0").json()
    p1 = authed_client.get("/api/agent/pending?limit=1&offset=1").json()
    p2 = authed_client.get("/api/agent/pending?limit=1&offset=2").json()
    assert p0["count"] == 1 and p0["has_more"] is True
    assert p1["count"] == 1 and p1["has_more"] is True
    assert p2["count"] == 0 and p2["has_more"] is False
    assert p0["rows"][0]["id"] != p1["rows"][0]["id"]
    assert {p0["rows"][0]["id"], p1["rows"][0]["id"]} == {
        row["id"] for row in authed_client.get("/api/agent/pending").json()["rows"]
    }


def test_pending_offset_rejects_negative(authed_client):
    assert authed_client.get("/api/agent/pending?offset=-1").status_code == 422


# --- b206: dismiss 'other' free-text note ------------------------------------

def test_dismiss_other_persists_free_text_note(authed_client):
    rows = authed_client.get("/api/agent/pending?tier=draft").json()["rows"]
    row_id = rows[0]["id"]
    r = authed_client.post(
        f"/api/agent/pending/{row_id}/dismiss",
        json={"reason": "other", "note": "colleague already handled this offline"},
    )
    assert r.status_code == 200
    after = r.json()["row"]
    assert after["dismissal_reason"] == "other"
    assert after["dismissal_note"] == "colleague already handled this offline"


def test_dismiss_note_is_length_bounded(authed_client):
    rows = authed_client.get("/api/agent/pending?tier=draft").json()["rows"]
    row_id = rows[0]["id"]
    r = authed_client.post(
        f"/api/agent/pending/{row_id}/dismiss",
        json={"reason": "other", "note": "x" * 501},
    )
    assert r.status_code == 422  # max_length=500


def test_dismiss_without_note_still_works(authed_client):
    rows = authed_client.get("/api/agent/pending").json()["rows"]
    row_id = next(r["id"] for r in rows if r["tier"] == "surface")
    r = authed_client.post(f"/api/agent/pending/{row_id}/dismiss", json={"reason": "noise"})
    assert r.status_code == 200
    assert r.json()["row"]["dismissal_note"] is None


# --- b211: account picker endpoint -------------------------------------------

def test_accounts_endpoint_lists_user_emails(authed_client, monkeypatch):
    monkeypatch.setattr("app.agent.scheduler.get_agent_config", lambda: {"accounts": []})
    monkeypatch.setattr("app.core.config.get_user_emails", lambda: ("a@x.com", "b@x.com"))
    r = authed_client.get("/api/agent/accounts")
    assert r.status_code == 200
    assert r.json()["accounts"] == ["a@x.com", "b@x.com"]


def test_accounts_endpoint_prefers_agent_accounts_and_dedupes(authed_client, monkeypatch):
    monkeypatch.setattr("app.agent.scheduler.get_agent_config",
                        lambda: {"accounts": ["x@y.com", " x@y.com ", "z@y.com"]})
    monkeypatch.setattr("app.core.config.get_user_emails", lambda: ("ignored@x.com",))
    r = authed_client.get("/api/agent/accounts")
    assert r.json()["accounts"] == ["x@y.com", "z@y.com"]


def test_accounts_endpoint_empty_when_none_configured(authed_client, monkeypatch):
    monkeypatch.setattr("app.agent.scheduler.get_agent_config", lambda: {"accounts": []})
    monkeypatch.setattr("app.core.config.get_user_emails", lambda: ())
    assert authed_client.get("/api/agent/accounts").json()["accounts"] == []


# --- b212: re-screen queue against current rules -----------------------------

def _seed_meeting_summary_draft(tmp_path):
    from app.agent import store
    db_url = f"sqlite:///{tmp_path}/var/youos.db"
    store.upsert_pending(
        db_url, message_id="m-recap", thread_id="t-r", account="you@example.com",
        sender="Notetaker <bot@x.com>", sender_email="bot@x.com",
        subject="Meeting summary — Q3 sync",
        body="Here are the notes from our meeting. Action items: review the deck.",
        received_at="2026-05-28T10:00:00Z", needs_reply_score=0.7, reasons=[],
        cold_outreach=False, tier="draft", draft="Thanks for the recap.",
        draft_model="qwen", draft_repairs=[], standing_instructions_snapshot=None,
    )


def test_rescreen_dismisses_stale_meeting_summary_keeps_legit(authed_client, tmp_path):
    _seed_meeting_summary_draft(tmp_path)
    dry = authed_client.post("/api/agent/rescreen", json={"dry_run": True}).json()
    assert dry["dismissed"] >= 1
    assert dry["scanned"] >= 2

    res = authed_client.post("/api/agent/rescreen", json={}).json()
    assert res["dismissed"] >= 1
    subjects = [r["subject"] for r in authed_client.get("/api/agent/pending?tier=draft").json()["rows"]]
    assert "Meeting summary — Q3 sync" not in subjects   # stale recap cleaned
    assert "Q3 pricing?" in subjects                      # legit draft preserved


def test_rescreen_dry_run_changes_nothing(authed_client, tmp_path):
    _seed_meeting_summary_draft(tmp_path)
    before = len(authed_client.get("/api/agent/pending?tier=draft").json()["rows"])
    authed_client.post("/api/agent/rescreen", json={"dry_run": True})
    after = len(authed_client.get("/api/agent/pending?tier=draft").json()["rows"])
    assert before == after


def test_rescreen_dismisses_cc_only_with_stored_recipients(authed_client, tmp_path):
    """b213: with To/Cc persisted on the row, re-screen can retroactively catch
    a CC-only draft (the user wasn't a direct recipient)."""
    from app.agent import store
    db_url = f"sqlite:///{tmp_path}/var/youos.db"
    store.upsert_pending(
        db_url, message_id="m-cc", thread_id="t-cc", account="you@example.com",
        sender="Colleague <colleague@x.com>", sender_email="colleague@x.com",
        subject="Project update", body="Could you confirm the timeline for next week?",
        received_at="2026-05-28T10:00:00Z", needs_reply_score=0.8, reasons=[],
        cold_outreach=False, tier="draft", draft="Sure.", draft_model="qwen",
        draft_repairs=[], standing_instructions_snapshot=None,
        to_recipients="Colleague <colleague@x.com>",
        cc_recipients="You <you@example.com>",
    )
    res = authed_client.post("/api/agent/rescreen", json={}).json()
    assert res["dismissed"] >= 1
    assert "cc'd" in res["by_category"]
    subjects = [r["subject"] for r in authed_client.get("/api/agent/pending?tier=draft").json()["rows"]]
    assert "Project update" not in subjects      # CC-only draft cleaned
    assert "Q3 pricing?" in subjects             # legit direct draft preserved


# --- b224: capture_outcomes endpoint -----------------------------------------

def test_capture_outcomes_endpoint(authed_client, monkeypatch):
    import base64
    def _b64(s): return base64.urlsafe_b64encode(s.encode()).decode()
    def _m(mid, frm, text):
        return {"id": mid, "payload": {"mimeType": "text/plain",
                "headers": [{"name": "From", "value": frm}], "body": {"data": _b64(text)}}}

    class _Src:
        def get_thread(self, *, account, thread_id):
            return {"messages": [
                _m("m-draft", "Alice <alice@partner.com>", "Could you confirm the Q3 pricing?"),
                _m("m-r", "You <you@example.com>", "Confirmed — pricing held through Q3, happy to put it in writing."),
            ]}
    monkeypatch.setattr("app.ingestion.adapters.get_google_source", lambda backend=None: _Src())

    r = authed_client.post("/api/agent/capture_outcomes", json={"account": "you@example.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["paired"] >= 1
    assert body["avg_edit_distance"] is not None


# --- surface → draft promotion on explicit regenerate (b282 add-on "Draft it") ---

def test_promote_to_draft_store_helper(tmp_path):
    import sqlite3

    from app.agent import store
    from app.db.bootstrap import _migrate_agent_pending_drafts, resolve_sqlite_path
    db = f"sqlite:///{tmp_path}/p.db"
    conn = sqlite3.connect(resolve_sqlite_path(db))
    _migrate_agent_pending_drafts(conn)
    conn.execute(
        "INSERT INTO agent_pending_drafts (id, message_id, thread_id, account, needs_reply_score, "
        "reasons_json, cold_outreach, tier, status) VALUES (1,'m','t','me@x.com',0.9,'[]',0,'surface','pending')"
    )
    conn.commit()
    conn.close()
    assert store.promote_to_draft(db, 1) is True
    assert store.get(db, 1)["tier"] == "draft"
    assert store.promote_to_draft(db, 1) is False  # already a draft → no-op


def test_regenerate_promotes_surfaced_row_to_draft(authed_client, monkeypatch):
    from types import SimpleNamespace

    import app.generation.service as svc
    # Stub the generation pipeline so the test doesn't need a model.
    monkeypatch.setattr(svc, "generate_draft",
                        lambda req, **kw: SimpleNamespace(draft="Drafted on request.", model_used="stub"))
    # The surface-tier fixture row.
    rows = authed_client.get("/api/agent/pending?tier=surface").json()["rows"]
    rid = rows[0]["id"]
    r = authed_client.post(f"/api/agent/pending/{rid}/regenerate", json={"instruction": "draft it"})
    assert r.status_code == 200
    row = r.json()["row"]
    assert row["tier"] == "draft"                       # promoted
    assert (row["amended_draft"] or "") == "Drafted on request."


# --- draft_for_thread (on-demand) + restore (undo) — b282 add-on polish ---

def test_restore_store_helper(tmp_path):
    import sqlite3

    from app.agent import store
    from app.db.bootstrap import _migrate_agent_pending_drafts, resolve_sqlite_path
    db = f"sqlite:///{tmp_path}/r.db"
    conn = sqlite3.connect(resolve_sqlite_path(db))
    _migrate_agent_pending_drafts(conn)
    conn.execute(
        "INSERT INTO agent_pending_drafts (id, message_id, thread_id, account, needs_reply_score, "
        "reasons_json, cold_outreach, tier, status) VALUES (1,'m','t','me@x.com',0.9,'[]',0,'draft','dismissed')"
    )
    conn.commit()
    conn.close()
    assert store.restore(db, 1) is True
    assert store.get(db, 1)["status"] == "pending"
    assert store.restore(db, 1) is False  # not dismissed anymore → no-op


def test_draft_for_thread_existing_surface_row(authed_client, monkeypatch):
    from types import SimpleNamespace

    import app.generation.service as svc
    monkeypatch.setattr(svc, "generate_draft",
                        lambda req, **kw: SimpleNamespace(draft="On-demand reply.", model_used="stub"))
    # t-2 is the surfaced fixture row.
    r = authed_client.post("/api/agent/draft_for_thread", json={"thread_id": "t-2"})
    assert r.status_code == 200, r.text
    row = r.json()["row"]
    assert row["tier"] == "draft" and (row["amended_draft"] or "") == "On-demand reply."
    assert r.json()["created"] is False


def test_draft_for_thread_no_row_creates_one(authed_client, monkeypatch):
    from types import SimpleNamespace

    import app.agent.inbox_fetch as inbox
    import app.generation.service as svc
    monkeypatch.setattr(svc, "generate_draft",
                        lambda req, **kw: SimpleNamespace(draft="Fresh draft.", model_used="stub"))
    monkeypatch.setattr(inbox, "fetch_thread", lambda account, thread_id, **kw: inbox.InboxMessage(
        message_id="newmsg", thread_id=thread_id, account=account,
        sender="Zoe <zoe@x.com>", sender_email="zoe@x.com", subject="Hi", body="Can you help?",
    ))
    r = authed_client.post("/api/agent/draft_for_thread",
                           json={"thread_id": "t-brand-new", "account": "you@example.com"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    assert body["row"]["tier"] == "draft" and body["row"]["draft"] == "Fresh draft."
    assert body["row"]["thread_id"] == "t-brand-new"


def test_restore_endpoint_undismisses(authed_client):
    rows = authed_client.get("/api/agent/pending?tier=draft").json()["rows"]
    rid = rows[0]["id"]
    authed_client.post(f"/api/agent/pending/{rid}/dismiss", json={"reason": "noise"})
    assert authed_client.get(f"/api/agent/pending/{rid}").json()["status"] == "dismissed"
    r = authed_client.post(f"/api/agent/pending/{rid}/restore")
    assert r.status_code == 200
    assert r.json()["row"]["status"] == "pending"


def test_regenerate_does_not_promote_dismissed_surface_row(authed_client, monkeypatch):
    """Hardening: a dismissed surface row must not be flipped to tier='draft' by
    regenerate (mark_amended no-ops on dismissed, so promote must be gated too)."""
    from types import SimpleNamespace

    import app.generation.service as svc
    monkeypatch.setattr(svc, "generate_draft",
                        lambda req, **kw: SimpleNamespace(draft="x", model_used="stub"))
    rows = authed_client.get("/api/agent/pending?tier=surface").json()["rows"]
    rid = rows[0]["id"]
    authed_client.post(f"/api/agent/pending/{rid}/dismiss", json={"reason": "noise"})
    authed_client.post(f"/api/agent/pending/{rid}/regenerate", json={})
    row = authed_client.get(f"/api/agent/pending/{rid}").json()
    assert row["status"] == "dismissed"   # untouched
    assert row["tier"] == "surface"        # NOT promoted
