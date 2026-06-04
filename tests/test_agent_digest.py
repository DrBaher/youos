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
    from datetime import datetime, timedelta, timezone

    from app.agent import store

    # Sweep timestamp must stay inside the digest's `days=7` window, so anchor it
    # relative to *now* rather than a hardcoded date — a fixed date silently
    # falls out of the window as the clock advances (the window-filtered
    # auto_promoted aggregate then goes empty and these tests time-bomb).
    sweep_started = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    sweep_finished = (datetime.now(timezone.utc) - timedelta(days=1) + timedelta(seconds=30)).isoformat()

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
        started_at=sweep_started,
        finished_at=sweep_finished,
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


def test_summary_line_fits_chat_bubble(db_url):
    """b59: the one-liner the orchestrator paraphrases must be ≤120 chars
    so it fits a Telegram/WhatsApp bubble cleanly."""
    _seed(db_url)
    from app.agent.digest import build_digest, summary_line

    d = build_digest(database_url=db_url, account="you@x.com", days=7)
    line = summary_line(d)
    assert len(line) <= 120
    assert "YouOS" in line
    assert "pending" in line
    assert "dismissed" in line


def test_chat_format_includes_pending_row_ids_for_action_targeting(db_url):
    """b59: the chat format must surface row ids so an orchestrator can
    issue follow-up POSTs (.../pending/{id}/push_to_gmail, etc.).
    Without ids, 'push #12' has nothing to dispatch to."""
    _seed(db_url)
    from app.agent.digest import build_digest, format_digest

    d = build_digest(database_url=db_url, account="you@x.com", days=7)
    chat = format_digest(d, fmt="chat")
    # First line is the headline summary.
    assert chat.startswith("YouOS")
    # Pending rows are listed with row-id prefix for action handle.
    assert "#" in chat
    # The headline contains the counts the orchestrator paraphrases.
    assert "pending" in chat


def test_json_format_includes_summary_field(db_url):
    """b59: orchestrators read the ``summary`` field first to emit a
    single-bubble headline; expose it at the top level."""
    _seed(db_url)
    from app.agent.digest import build_digest, format_digest

    d = build_digest(database_url=db_url, account="you@x.com", days=7)
    out = format_digest(d, fmt="json")
    parsed = json.loads(out)
    assert "summary" in parsed
    assert parsed["summary"].startswith("YouOS")


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


def test_digest_reports_auto_sent_and_shadow_counts(db_url):
    """The accountability report surfaces what the send frontier actually did:
    auto-sent vs shadow-sent (soak), derived from send_state."""
    import sqlite3

    from app.agent.digest import build_digest, format_digest

    path = db_url.removeprefix("sqlite:///")
    conn = sqlite3.connect(path)
    for i, send_state in enumerate(["sent", "shadow"]):
        conn.execute(
            "INSERT INTO agent_pending_drafts "
            "(message_id, thread_id, account, sender_email, needs_reply_score, "
            " reasons_json, cold_outreach, tier, draft, status, gmail_draft_id, "
            " send_state, actually_sent_at) "
            "VALUES (?, ?, 'you@x.com', 's@x.com', 0.9, '[]', 0, 'draft', 'hi', "
            " 'sent', ?, ?, datetime('now'))",
            (f"as{i}", f"ast{i}", f"gd{i}", send_state),
        )
    conn.commit()
    conn.close()

    d = build_digest(database_url=db_url, account="you@x.com", days=7)
    assert d.auto_sent_count == 1
    assert d.shadow_sent_count == 1
    text = format_digest(d, fmt="text")
    assert "Auto-sent: 1" in text
    assert "Shadow-sent" in text
    parsed = json.loads(format_digest(d, fmt="json"))
    assert parsed["auto_sent_count"] == 1 and parsed["shadow_sent_count"] == 1


def test_pending_preview_includes_draft_body_for_notifications(db_url):
    """The webhook/JSON digest carries the draft body inline so a notification
    (OpenClaw) can show 'the email and the reply' without a callback."""
    _seed(db_url)
    from app.agent.digest import build_digest, format_digest

    d = build_digest(database_url=db_url, account="you@x.com", days=7)
    assert d.pending_preview, "expected at least one pending row"
    assert all("draft" in p for p in d.pending_preview)
    # The draft text rides along in the JSON payload an orchestrator consumes.
    parsed = json.loads(format_digest(d, fmt="json"))
    assert "draft" in parsed["pending_preview"][0]
