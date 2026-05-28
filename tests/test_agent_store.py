"""DAL for the agent_pending_drafts table — insert idempotency, list filters,
state transitions (amend / sent / dismissed)."""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture
def db_url(tmp_path):
    db = tmp_path / "agent.db"
    conn = sqlite3.connect(db)
    from app.db.bootstrap import _migrate_agent_pending_drafts

    _migrate_agent_pending_drafts(conn)
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


def test_list_pending_default_excludes_non_pending(db_url):
    from app.agent import store

    row_id = store.upsert_pending(db_url, **_DEFAULTS)
    store.upsert_pending(db_url, **{**_DEFAULTS, "message_id": "m-2"})
    store.mark_dismissed(db_url, row_id)
    rows = store.list_pending(db_url)  # default status='pending'
    assert {r["message_id"] for r in rows} == {"m-2"}
