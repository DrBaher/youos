"""DAL for the agent_pending_drafts table — insert idempotency, list filters,
state transitions (amend / sent / dismissed)."""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture
def db_url(tmp_path):
    db = tmp_path / "agent.db"
    conn = sqlite3.connect(db)
    from app.db.bootstrap import _migrate_agent_audit, _migrate_agent_pending_drafts

    _migrate_agent_pending_drafts(conn)
    _migrate_agent_audit(conn)
    conn.commit()
    conn.close()
    return f"sqlite:///{db}"


_DEFAULTS = dict(
    message_id="m-1",
    thread_id="t-1",
    account="you@example.com",
    sender="Alice <alice@partner.com>",
    sender_email="alice@partner.com",
    subject="Pricing",
    body="Q3 pricing?",
    received_at="2026-05-28T10:00:00Z",
    needs_reply_score=0.75,
    reasons=["ends with a question", "imperative verb present"],
    cold_outreach=False,
    tier="draft",
    draft="Confirmed — pricing unchanged.",
    draft_model="qwen2.5-1.5b-lora",
    draft_repairs=["stripped_trailing_signature"],
    standing_instructions_snapshot=None,
)


def test_upsert_inserts_then_ignores_duplicates(db_url):
    from app.agent import store

    row_id = store.upsert_pending(db_url, **_DEFAULTS)
    assert isinstance(row_id, int) and row_id > 0
    again = store.upsert_pending(db_url, **_DEFAULTS)
    assert again is None  # idempotent — same message_id


def test_list_pending_returns_newest_first_and_rehydrates_json(db_url):
    from app.agent import store

    store.upsert_pending(db_url, **{**_DEFAULTS, "message_id": "m-1", "needs_reply_score": 0.65})
    store.upsert_pending(db_url, **{**_DEFAULTS, "message_id": "m-2", "needs_reply_score": 0.85})
    rows = store.list_pending(db_url)
    assert len(rows) == 2
    # Sorted by score DESC then created_at — higher-confidence draft first.
    assert rows[0]["message_id"] == "m-2"
    # JSON columns rehydrated to lists.
    assert rows[0]["reasons"] == _DEFAULTS["reasons"]
    assert rows[0]["draft_repairs"] == _DEFAULTS["draft_repairs"]
    assert rows[0]["cold_outreach"] is False


def test_list_pending_filters_by_tier(db_url):
    from app.agent import store

    store.upsert_pending(db_url, **{**_DEFAULTS, "message_id": "m-1", "tier": "draft"})
    store.upsert_pending(db_url, **{**_DEFAULTS, "message_id": "m-2", "tier": "surface", "draft": None})
    drafts = store.list_pending(db_url, tier="draft")
    surface = store.list_pending(db_url, tier="surface")
    assert {r["message_id"] for r in drafts} == {"m-1"}
    assert {r["message_id"] for r in surface} == {"m-2"}


def test_state_transitions_amend_then_send(db_url):
    from app.agent import store

    row_id = store.upsert_pending(db_url, **_DEFAULTS)
    assert store.mark_amended(db_url, row_id, amended_draft="Confirmed — pricing held.")
    r = store.get(db_url, row_id)
    assert r["status"] == "amended"
    assert r["amended_draft"] == "Confirmed — pricing held."

    assert store.mark_sent(db_url, row_id)
    r = store.get(db_url, row_id)
    assert r["status"] == "sent"
    assert r["sent_at"] is not None


def test_dismiss_marks_dismissed(db_url):
    from app.agent import store

    row_id = store.upsert_pending(db_url, **_DEFAULTS)
    assert store.mark_dismissed(db_url, row_id)
    r = store.get(db_url, row_id)
    assert r["status"] == "dismissed"
    assert r["dismissed_at"] is not None
    # No reason supplied — column stays NULL ("no_reason" bucket in stats).
    assert r["dismissal_reason"] is None


def test_dismiss_records_categorical_reason(db_url):
    from app.agent import store

    row_id = store.upsert_pending(db_url, **_DEFAULTS)
    assert store.mark_dismissed(db_url, row_id, reason="noise")
    r = store.get(db_url, row_id)
    assert r["dismissal_reason"] == "noise"


def test_dismiss_coerces_unknown_reason_to_other(db_url):
    """Defence in depth — API rejects unknown reasons upstream, but if a
    legacy caller passes something we don't recognise the DAL keeps the
    column bounded by mapping it to 'other'."""
    from app.agent import store

    row_id = store.upsert_pending(db_url, **_DEFAULTS)
    assert store.mark_dismissed(db_url, row_id, reason="some_random_bucket")
    r = store.get(db_url, row_id)
    assert r["dismissal_reason"] == "other"


def test_dismissal_stats_aggregates_by_reason(db_url):
    from app.agent import store

    # 4 rows: 2 dismissed (noise + wrong_sender), 1 dismissed no-reason, 1 pending.
    for i, (reason, dismiss) in enumerate([
        ("noise", True),
        ("wrong_sender", True),
        (None, True),
        (None, False),
    ]):
        rid = store.upsert_pending(db_url, **{**_DEFAULTS, "message_id": f"m-{i}"})
        if dismiss:
            store.mark_dismissed(db_url, rid, reason=reason)

    stats = store.dismissal_stats(db_url)
    assert stats["total_persisted"] == 4
    assert stats["dismissed"] == 3
    assert stats["dismissal_rate"] == 0.75
    assert stats["by_reason"]["noise"] == 1
    assert stats["by_reason"]["wrong_sender"] == 1
    assert stats["by_reason"]["no_reason"] == 1
    assert stats["by_reason"]["wrong_content"] == 0  # zero-filled


def test_dismissal_stats_filters_by_account(db_url):
    from app.agent import store

    a = store.upsert_pending(db_url, **{**_DEFAULTS, "message_id": "m-a", "account": "a@x.com"})
    b = store.upsert_pending(db_url, **{**_DEFAULTS, "message_id": "m-b", "account": "b@x.com"})
    store.mark_dismissed(db_url, a, reason="noise")
    store.mark_dismissed(db_url, b, reason="wrong_content")

    only_a = store.dismissal_stats(db_url, account="a@x.com")
    assert only_a["total_persisted"] == 1
    assert only_a["dismissed"] == 1
    assert only_a["by_reason"]["noise"] == 1
    assert only_a["by_reason"]["wrong_content"] == 0


def test_list_pending_default_excludes_non_pending(db_url):
    from app.agent import store

    row_id = store.upsert_pending(db_url, **_DEFAULTS)
    store.upsert_pending(db_url, **{**_DEFAULTS, "message_id": "m-2"})
    store.mark_dismissed(db_url, row_id)
    rows = store.list_pending(db_url)  # default status='pending'
    assert {r["message_id"] for r in rows} == {"m-2"}


# --- ε: audit log ---------------------------------------------------------


def test_log_sweep_inserts_and_list_recent_orders_newest_first(db_url):
    from app.agent import store

    store.log_sweep(
        db_url, account="a@x.com", trigger="manual", window="3d", threshold=0.6,
        fetched=5, kept=2, surfaced=1, persisted=2, errors=[],
        standing_instructions_snapshot=None,
        started_at="2026-05-28T10:00:00Z", finished_at="2026-05-28T10:00:02Z",
        duration_ms=2000,
    )
    store.log_sweep(
        db_url, account="a@x.com", trigger="scheduled", window="24h", threshold=0.6,
        fetched=12, kept=0, surfaced=2, persisted=0,
        errors=["gog auth failed"], standing_instructions_snapshot="be brief",
        started_at="2026-05-28T11:00:00Z", finished_at="2026-05-28T11:00:01Z",
        duration_ms=1000,
    )

    sweeps = store.list_recent_sweeps(db_url)
    assert len(sweeps) == 2
    # Newest first.
    assert sweeps[0]["trigger"] == "scheduled"
    # JSON column rehydrated.
    assert sweeps[0]["errors"] == ["gog auth failed"]
    assert sweeps[1]["errors"] == []


def test_list_recent_sweeps_filters_by_account(db_url):
    from app.agent import store

    for acct in ("a@x.com", "b@y.com", "a@x.com"):
        store.log_sweep(
            db_url, account=acct, trigger="manual", window="3d", threshold=0.6,
            fetched=0, kept=0, surfaced=0, persisted=0, errors=[],
            standing_instructions_snapshot=None,
            started_at="2026-05-28T10:00:00Z", finished_at="2026-05-28T10:00:01Z",
            duration_ms=1000,
        )
    only_a = store.list_recent_sweeps(db_url, account="a@x.com")
    assert len(only_a) == 2
    assert all(s["account"] == "a@x.com" for s in only_a)
