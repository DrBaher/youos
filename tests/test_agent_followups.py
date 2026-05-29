"""Follow-up tracking: owed inbound + awaiting reply."""

from __future__ import annotations

import sqlite3

import pytest

from app.agent import followups, store


@pytest.fixture
def db_url(tmp_path):
    db = tmp_path / "fu.db"
    conn = sqlite3.connect(db)
    from app.db.bootstrap import _migrate_agent_audit, _migrate_agent_pending_drafts

    _migrate_agent_pending_drafts(conn)
    _migrate_agent_audit(conn)
    conn.commit()
    conn.close()
    return f"sqlite:///{db}"


def _seed(db_url, *, thread_id, message_id, account="you@x.com"):
    return store.upsert_pending(
        db_url,
        message_id=message_id, thread_id=thread_id, account=account,
        sender="Alice <alice@x.com>", sender_email="alice@x.com",
        subject="Q3 pricing", body="please confirm", received_at=None,
        needs_reply_score=0.8, reasons=[], cold_outreach=False,
        tier="draft", draft="hi", draft_model="m",
        draft_repairs=[], standing_instructions_snapshot=None,
    )


def _set(db_url, rid, **cols):
    """Backdate/override columns directly for deterministic age tests."""
    path = db_url.removeprefix("sqlite:///")
    conn = sqlite3.connect(path)
    try:
        sets = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(f"UPDATE agent_pending_drafts SET {sets} WHERE id = ?", (*cols.values(), rid))
        conn.commit()
    finally:
        conn.close()


def test_owed_inbound_flags_aging_pending_only(db_url):
    old = _seed(db_url, thread_id="t-old", message_id="m-old")
    _set(db_url, old, received_at="2026-05-01T09:00:00Z")  # weeks ago
    fresh = _seed(db_url, thread_id="t-fresh", message_id="m-fresh")
    _set(db_url, fresh, received_at="2099-01-01T09:00:00Z")  # future → not aged

    owed = followups.owed_inbound(db_url, account="you@x.com", owed_days=2)
    ids = [r["id"] for r in owed]
    assert old in ids
    assert fresh not in ids


def test_awaiting_reply_flags_old_sent_with_no_newer(db_url):
    rid = _seed(db_url, thread_id="t-await", message_id="m-await")
    _set(db_url, rid, status="sent", gmail_draft_id="g1", sent_at="2026-05-01T09:00:00Z")

    aw = followups.awaiting_reply(db_url, account="you@x.com", wait_days=4)
    assert [r["id"] for r in aw] == [rid]


def test_awaiting_excluded_when_newer_thread_activity(db_url):
    rid = _seed(db_url, thread_id="t-replied", message_id="m-sent")
    _set(db_url, rid, status="sent", gmail_draft_id="g2", sent_at="2026-05-01T09:00:00Z")
    # A newer inbound landed on the same thread (swept later) → they replied.
    newer = _seed(db_url, thread_id="t-replied", message_id="m-newer")
    _set(db_url, newer, created_at="2026-05-05 09:00:00")

    aw = followups.awaiting_reply(db_url, account="you@x.com", wait_days=4)
    assert rid not in [r["id"] for r in aw]


def test_build_followups_counts_and_shape(db_url):
    owed = _seed(db_url, thread_id="t1", message_id="m1")
    _set(db_url, owed, received_at="2026-05-01T09:00:00Z")
    awaiting = _seed(db_url, thread_id="t2", message_id="m2")
    _set(db_url, awaiting, status="sent", gmail_draft_id="g", sent_at="2026-05-01T09:00:00Z")

    fu = followups.build_followups(db_url, account="you@x.com")
    assert fu["owed_count"] == 1
    assert fu["awaiting_count"] == 1
    assert {"owed", "awaiting", "owed_days", "wait_days"} <= set(fu)
    assert fu["owed"][0]["age_days"] > 0
