"""b162: retention for the append-only agent tables + the autoresearch jsonl trim."""

from __future__ import annotations

import sqlite3

from app.agent.store import prune_agent_tables
from app.autoresearch.optimizer import _trim_jsonl
from app.db.bootstrap import _migrate_agent_audit, _migrate_agent_pending_drafts


def test_prune_removes_aged_keeps_live_and_recent(tmp_path):
    db = tmp_path / "agent.db"
    conn = sqlite3.connect(db)
    _migrate_agent_pending_drafts(conn)
    _migrate_agent_audit(conn)

    def add_draft(mid, status, days_ago, mined=1):
        conn.execute(
            "INSERT INTO agent_pending_drafts (message_id, thread_id, account, needs_reply_score, tier, status, created_at, feedback_captured) "
            "VALUES (?, 't', 'a@x.com', 0.5, 'draft', ?, datetime('now', ?), ?)",
            (mid, status, f"-{days_ago} days", mined),
        )

    add_draft("old-dismissed", "dismissed", 100)  # aged terminal, mined -> pruned
    # b244: status='sent' with send_state NULL is recipient_trust evidence
    # (user-confirmed send) — kept indefinitely now.
    add_draft("old-sent", "sent", 100)
    add_draft("old-pending", "pending", 100)      # aged but LIVE -> kept
    add_draft("new-dismissed", "dismissed", 5)    # recent terminal -> kept
    conn.execute("INSERT INTO agent_audit (account, trigger, started_at) VALUES ('a@x.com','scheduled',datetime('now','-100 days'))")
    conn.execute("INSERT INTO agent_audit (account, trigger, started_at) VALUES ('a@x.com','scheduled',datetime('now','-5 days'))")
    conn.commit()
    conn.close()

    removed = prune_agent_tables(f"sqlite:///{db}", older_than_days=90)
    assert removed["agent_pending_drafts"] == 1  # old-dismissed only (b244: old-sent = trust evidence)
    assert removed["agent_audit"] == 1

    conn = sqlite3.connect(db)
    surviving = {r[0] for r in conn.execute("SELECT message_id FROM agent_pending_drafts")}
    assert surviving == {"old-pending", "new-dismissed", "old-sent"}  # live-aged + recent-terminal + trust kept
    assert conn.execute("SELECT COUNT(*) FROM agent_audit").fetchone()[0] == 1
    conn.close()


def test_prune_is_safe_on_missing_tables(tmp_path):
    """A schema-stale instance (only one table) prunes what it can, no crash."""
    db = tmp_path / "agent.db"
    conn = sqlite3.connect(db)
    _migrate_agent_audit(conn)
    conn.commit()
    conn.close()
    removed = prune_agent_tables(f"sqlite:///{db}", older_than_days=90)
    assert removed["agent_audit"] == 0       # present, nothing aged
    assert removed["draft_events"] == 0      # absent -> 0, not an error


def test_trim_jsonl_keeps_last_n(tmp_path):
    p = tmp_path / "runs.jsonl"
    p.write_text("".join(f'{{"i": {i}}}\n' for i in range(500)))
    _trim_jsonl(p, 100)
    lines = p.read_text().splitlines()
    assert len(lines) == 100
    assert lines[-1] == '{"i": 499}'  # newest kept
    assert lines[0] == '{"i": 400}'   # oldest dropped
    assert not (p.parent / "runs.jsonl.tmp").exists()  # atomic temp cleaned up


def test_trim_jsonl_noop_under_cap(tmp_path):
    p = tmp_path / "runs.jsonl"
    p.write_text('{"i": 1}\n{"i": 2}\n')
    _trim_jsonl(p, 100)
    assert p.read_text() == '{"i": 1}\n{"i": 2}\n'  # unchanged
