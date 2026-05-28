"""Daily-digest builder + formatters — b56.

Tests use the same agent_pending_drafts + agent_audit fixtures the rest of
the agent test suite uses. The digest is pure formatting on top of existing
store helpers, so the test surface is just "given known store state, the
digest reflects it accurately in each format."
"""

from __future__ import annotations

import json
import sqlite3

import pytest


@pytest.fixture
def db_url(tmp_path):
    db = tmp_path / "digest.db"
    conn = sqlite3.connect(db)
    from app.db.bootstrap import _migrate_agent_audit, _migrate_agent_pending_drafts

    _migrate_agent_pending_drafts(conn)
    _migrate_agent_audit(conn)
    conn.commit()
    conn.close()
    return f"sqlite:///{db}"


def _seed(db_url):
    """Seed enough rows + audit entries that the digest has interesting numbers."""
    from app.agent import store

    # Pending drafts (2) + a sent draft + a dismissed-as-noise row.
    # `_status` is the final lifecycle state we'll transition rows into
    # below — kept in the tuple for readability though only `tier` and
    # `subject` go onto upsert_pending.
    rids = []
    for i, (tier, subject, _status) in enumerate([
        ("draft", "Q3 pricing", "pending"),
        ("draft", "Re: contract", "pending"),
        ("draft", "FYI logistics", "sent"),
        ("surface", "deploy failed", "dismissed"),
    ]):
        rid = store.upsert_pending(db_url, **{
            "message_id": f"m-{i}", "thread_id": f"t-{i}", "account": "you@x.com",
            "sender": f"Sender {i}", "sender_email": f"s{i}@x.com",
            "subject": subject, "body": "y", "received_at": None,
            "needs_reply_score": 0.7, "reasons": [], "cold_outreach": False,
            "tier": tier, "draft": "hi" if tier == "draft" else None,
            "draft_model": "qwen" if tier == "draft" else None,
            "draft_repairs": [], "standing_instructions_snapshot": None,
        })
        rids.append(rid)

    # Lifecycle: row 3 -> sent (with gmail_draft_id), row 4 -> dismissed as noise
    store.mark_sent(db_url, rids[2], gmail_draft_id="r123")
    store.mark_dismissed(db_url, rids[3], reason="noise")

    # Two sweeps (one auto-promoted a sender).
    store.log_sweep(
        db_url, account="you@x.com", trigger="scheduled", window="24h", threshold=0.6,
        fetched=20, kept=3, surfaced=1, persisted=4, errors=[],
        standing_instructions_snapshot=None,
        started_at="2026-05-28T08:00:00+00:00",
        finished_at="2026-05-28T08:00:30+00:00",
        duration_ms=30000,
        auto_promoted_senders=["spam@noise.com"],
    )


def test_build_digest_summarizes_seeded_state(db_url):
    _seed(db_url)
    from app.agent.digest import build_digest

    d = build_digest(database_url=db_url, account="you@x.com", days=7)
    assert d.account == "you@x.com"
    assert d.sweeps == 1
    assert d.sweeps_successful == 1
    assert d.fetched == 20
    assert d.hard_skipped == 17        # 20 fetched - 3 kept
    # 1 row pending, 1 sent w/ gmail_draft_id, 1 dismissed → counts reflect that
    assert d.pending_count == 2        # rows 0 + 1 (still pending)
    assert d.pushed_count == 1         # row 2 (sent + gmail_draft_id)
    assert d.dismissed_count == 1
    assert d.dismissal_by_reason == {"noise": 1}
    assert d.auto_promoted == ["spam@noise.com"]
    assert any(t["sender_email"] == "s3@x.com" for t in d.top_noise_senders)


def test_format_text_includes_key_signals(db_url):
    _seed(db_url)
    from app.agent.digest import build_digest, format_digest

    d = build_digest(database_url=db_url, account="you@x.com", days=7)
    out = format_digest(d, fmt="text")
    assert "Agent digest for you@x.com" in out
    assert "Sweeps:" in out
    assert "Auto-promoted to skip_senders" in out
    assert "spam@noise.com" in out
    assert "Top dismissed-as-noise senders" in out


def test_format_json_is_parseable_and_round_trips_data(db_url):
    _seed(db_url)
    from app.agent.digest import build_digest, format_digest

    d = build_digest(database_url=db_url, account="you@x.com", days=7)
    out = format_digest(d, fmt="json")
    parsed = json.loads(out)
    assert parsed["sweeps"] == d.sweeps
    assert parsed["auto_promoted"] == d.auto_promoted


def test_format_html_renders_skeleton_markup(db_url):
    _seed(db_url)
    from app.agent.digest import build_digest, format_digest

    d = build_digest(database_url=db_url, account="you@x.com", days=7)
    out = format_digest(d, fmt="html")
    assert out.startswith("<!doctype html>")
    assert "<table" in out
    assert "spam@noise.com" in out


def test_empty_state_produces_clean_digest(db_url):
    """No audit rows, no pending — digest should still be valid + parseable."""
    from app.agent.digest import build_digest, format_digest

    d = build_digest(database_url=db_url, account="empty@x.com", days=1)
    assert d.sweeps == 0
    assert d.pending_count == 0
    assert d.dismissed_count == 0
    text = format_digest(d, fmt="text")
    assert "Agent digest for empty@x.com" in text
    parsed = json.loads(format_digest(d, fmt="json"))
    assert parsed["sweeps"] == 0
