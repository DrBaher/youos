"""The agent self-heals a stale instance DB before sweeping.

Reproduces the b85 production failure: an existing instance DB that predates a
new column made EVERY sweep crash at the persist step ('no column named
quality_score') with zero drafts, invisibly. ensure_agent_schema (called at
run_triage start) must upgrade the DB in place — no schema.sql needed.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.db.bootstrap import ensure_agent_schema

# The agent_pending_drafts shape BEFORE the autonomy work (pre-b85): no
# quality_score / calibrated_score / send_state / hold / amended_by / etc.
_OLD_SCHEMA = """
CREATE TABLE agent_pending_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL,
    account TEXT NOT NULL,
    sender TEXT, sender_email TEXT, subject TEXT, body TEXT, received_at TEXT,
    needs_reply_score REAL NOT NULL,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    cold_outreach INTEGER NOT NULL DEFAULT 0,
    tier TEXT NOT NULL,
    draft TEXT, draft_model TEXT,
    draft_repairs_json TEXT NOT NULL DEFAULT '[]',
    standing_instructions_snapshot TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    amended_draft TEXT, sent_at TEXT, dismissed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_NEW_COLS = [
    "gmail_draft_id", "dismissal_reason", "thread_summary", "quality_score",
    "calibrated_score", "send_state", "sent_message_id", "actually_sent_at",
    "feedback_captured", "amended_by", "hold",
]


def _old_db(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY, inbound_author TEXT)")
    conn.executescript(_OLD_SCHEMA)
    conn.commit()
    conn.close()
    return db


def test_ensure_agent_schema_upgrades_a_stale_db(tmp_path):
    db = _old_db(tmp_path)
    url = f"sqlite:///{db}"
    # Pre-condition: the column that crashed production is missing.
    cols0 = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(agent_pending_drafts)").fetchall()}
    assert "quality_score" not in cols0

    assert ensure_agent_schema(url) is True

    cols1 = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(agent_pending_drafts)").fetchall()}
    for c in _NEW_COLS:
        assert c in cols1, f"{c} not added by ensure_agent_schema"
    # And it created the sibling agent tables too.
    tables = {r[0] for r in sqlite3.connect(db).execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "agent_audit" in tables
    assert "triage_precision_history" in tables


def test_ensure_agent_schema_is_idempotent(tmp_path):
    db = _old_db(tmp_path)
    url = f"sqlite:///{db}"
    assert ensure_agent_schema(url) is True
    assert ensure_agent_schema(url) is True  # second call must not raise


def test_run_triage_self_heals_and_drafts_on_a_stale_db(tmp_path, monkeypatch):
    """End-to-end: run_triage against a pre-b85 DB succeeds (no
    'no column named quality_score') because it migrates first."""
    from app.agent import triage
    from app.agent.inbox_fetch import InboxMessage

    db = _old_db(tmp_path)
    url = f"sqlite:///{db}"

    msg = InboxMessage(
        message_id="m1", thread_id="t1", account="you@example.com",
        sender="Alice <alice@partner.com>", sender_email="alice@partner.com",
        subject="Pricing question", body="Could you confirm the Q3 pricing?",
        headers={},
    )
    monkeypatch.setattr("app.agent.triage.fetch_unread", lambda *a, **k: [msg])

    class _Resp:
        draft = "Hi Alice, confirmed."
        model_used = "qwen2.5-1.5b-lora"
        repairs: list[str] = []
        quality_score = 0.8

    monkeypatch.setattr("app.generation.service.generate_draft", lambda req, **kw: _Resp())

    result = triage.run_triage(
        account="you@example.com", database_url=url, configs_dir=tmp_path,
    )
    # Before the fix this raised OperationalError and persisted 0; now it drafts.
    assert result.kept == 1
    assert result.persisted == 1
    # The row landed with the new columns populated.
    row = sqlite3.connect(db).execute(
        "SELECT quality_score, hold FROM agent_pending_drafts WHERE message_id='m1'"
    ).fetchone()
    assert row[0] == pytest.approx(0.8)
    assert row[1] == 0