"""b185: real draft<->sent capture + honest learning signal.

The self-improvement loop's feedback signal was hollow: ~82k organic backfill
rows (sent reply copied into BOTH generated_draft and edited_reply with a
hardcoded edit_distance_pct=0.0) made avg_edit_distance read 0.0 everywhere — a
false "drafts are perfect" reading that collapsed the autoresearch
draft-quality weighting to uniform. These tests prove:

  (i)   an organic sent-mail row with no prior agent draft is NOT counted in the
        learning/outcome join and fabricates no ed=0 comparison;
  (ii)  when a prior agent draft (logged in draft_events) differs from the later
        sent reply, a REAL feedback pair is captured (organic=0) with a real
        edit_distance_pct computed by similarity_ratio, and it DOES join in
        summarize_draft_events with that real distance;
  (iii) the autoresearch draft-quality weighting receives real, non-uniform
        per-sender_type distances from that data — the path is no longer inert.

All record-only: nothing here changes when/whether the agent drafts or sends.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.autoresearch.scorer import draft_quality_case_weights
from app.core.diff import similarity_ratio
from app.core.stats import summarize_draft_events
from app.db.bootstrap import resolve_sqlite_path
from scripts.extract_auto_feedback import _capture_organic_pairs


def _make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE reply_pairs (
            id INTEGER PRIMARY KEY,
            inbound_text TEXT,
            reply_text TEXT,
            source_type TEXT DEFAULT 'email',
            source_id TEXT DEFAULT '',
            created_ts TEXT DEFAULT CURRENT_TIMESTAMP,
            auto_feedback_processed INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE feedback_pairs (
            id INTEGER PRIMARY KEY,
            inbound_text TEXT,
            generated_draft TEXT,
            edited_reply TEXT,
            feedback_note TEXT,
            edit_distance_pct REAL,
            rating INTEGER,
            used_in_finetune INTEGER DEFAULT 0,
            reply_pair_id INTEGER,
            organic BOOLEAN DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        """CREATE TABLE draft_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inbound_text TEXT NOT NULL, generated_draft TEXT NOT NULL,
            sender_type TEXT, confidence TEXT, length_flag TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.commit()
    return conn


def _db_url(tmp_path) -> tuple[str, Path]:
    db_path = tmp_path / "youos.db"
    for url in (f"sqlite:///{db_path}", str(db_path), db_path.as_uri()):
        try:
            if resolve_sqlite_path(url) == db_path:
                return url, db_path
        except Exception:  # noqa: BLE001
            continue
    raise AssertionError(f"no database_url form resolved to {db_path}")


# --- (i) organic sent-mail with no prior draft -> not a comparison ----------


def test_organic_no_prior_draft_is_not_a_comparison(tmp_path):
    url, db_path = _db_url(tmp_path)
    conn = _make_db(db_path)
    try:
        # A sent reply with inbound context but NO prior agent draft logged.
        conn.execute(
            "INSERT INTO reply_pairs (inbound_text, reply_text, auto_feedback_processed) VALUES (?, ?, 0)",
            ("Can you send the Q3 numbers?", "Sure — attached the Q3 deck, let me know if you need the raw sheet."),
        )
        conn.commit()

        count = _capture_organic_pairs(conn, dry_run=False)
        conn.commit()
        assert count == 1

        # It was stored as organic (recorded) — but with no model draft to diff.
        row = conn.execute(
            "SELECT organic, edit_distance_pct, generated_draft, edited_reply, feedback_note FROM feedback_pairs"
        ).fetchone()
        organic, ed, gen, edited, note = row
        assert organic == 1
        assert gen == edited  # sent reply copied into both -> no real comparison
        assert "organic" in note
    finally:
        conn.close()

    # And it does NOT count as a draft-quality comparison in the learning join.
    # (There's a matching draft_events? No — none was logged. matched stays 0.)
    outcome = summarize_draft_events(url)["outcome"]
    assert outcome["matched"] == 0
    assert outcome["avg_edit_distance_by_sender_type"] == {}


# --- (ii) prior agent draft differs from sent reply -> real captured pair ----


def test_prior_draft_yields_real_captured_pair(tmp_path):
    url, db_path = _db_url(tmp_path)
    conn = _make_db(db_path)
    inbound = "Are we still on for the kickoff Thursday?"
    agent_draft = "Yes, Thursday works — I'll send a calendar invite shortly."
    user_sent = "Thursday's tight for me; can we push the kickoff to Friday morning instead?"
    try:
        # The agent had ALREADY drafted a reply for this inbound (draft_events).
        conn.execute(
            "INSERT INTO draft_events (inbound_text, generated_draft, sender_type, confidence) VALUES (?, ?, ?, ?)",
            (inbound, agent_draft, "external_client", "high"),
        )
        # The user later sent a materially different reply (organic ingestion).
        conn.execute(
            "INSERT INTO reply_pairs (inbound_text, reply_text, auto_feedback_processed) VALUES (?, ?, 0)",
            (inbound, user_sent),
        )
        conn.commit()

        count = _capture_organic_pairs(conn, dry_run=False)
        conn.commit()
        assert count == 1

        row = conn.execute(
            "SELECT organic, edit_distance_pct, generated_draft, edited_reply FROM feedback_pairs"
        ).fetchone()
        organic, ed, gen, edited = row
        # Captured as a GENUINE comparison: non-organic, real distance, the
        # agent's draft vs the user's actual sent text.
        assert organic == 0
        assert gen == agent_draft
        assert edited == user_sent
        expected_ed = round(1.0 - similarity_ratio(agent_draft, user_sent), 4)
        assert ed == pytest.approx(expected_ed, abs=1e-9)
        assert ed > 0.0  # they genuinely differ
    finally:
        conn.close()

    # And it JOINS in summarize_draft_events with that REAL distance (the join
    # key is inbound_text, shared by draft_events and feedback_pairs).
    outcome = summarize_draft_events(url)["outcome"]
    assert outcome["matched"] == 1
    ext = outcome["avg_edit_distance_by_sender_type"]["external_client"]
    assert ext["avg_edit_distance"] == pytest.approx(round(expected_ed, 3), abs=1e-3)
    assert ext["n"] == 1


def test_prior_draft_sent_verbatim_stays_organic(tmp_path):
    """If the agent drafted it and the user sent EXACTLY that, there's no edit to
    learn from — it falls back to an organic (excluded) row, not a fake real
    comparison."""
    url, db_path = _db_url(tmp_path)
    conn = _make_db(db_path)
    inbound = "Thanks for the update."
    text = "You're welcome — glad it helped. Reach out anytime."
    try:
        conn.execute(
            "INSERT INTO draft_events (inbound_text, generated_draft, sender_type, confidence) VALUES (?, ?, ?, ?)",
            (inbound, text, "internal", "high"),
        )
        conn.execute(
            "INSERT INTO reply_pairs (inbound_text, reply_text, auto_feedback_processed) VALUES (?, ?, 0)",
            (inbound, text),
        )
        conn.commit()
        _capture_organic_pairs(conn, dry_run=False)
        conn.commit()
        organic = conn.execute("SELECT organic FROM feedback_pairs").fetchone()[0]
        assert organic == 1  # verbatim -> organic, excluded from learning
    finally:
        conn.close()
    assert summarize_draft_events(url)["outcome"]["matched"] == 0


# --- (iii) weighting is no longer inert -------------------------------------


def test_real_distances_drive_nonuniform_weights(tmp_path):
    """Two cohorts with DIFFERENT real edit distances (captured via the Tier 2
    path) produce non-uniform draft_quality_case_weights — proving the
    autoresearch weighting is no longer collapsed to a uniform 1.0 by the
    all-zero organic backfill."""
    url, db_path = _db_url(tmp_path)
    conn = _make_db(db_path)
    try:
        # external_client: agent nearly nailed it (small edit).
        conn.execute(
            "INSERT INTO draft_events (inbound_text, generated_draft, sender_type, confidence) VALUES (?, ?, ?, ?)",
            ("ext inbound", "Confirmed, see you at 2pm.", "external_client", "high"),
        )
        conn.execute(
            "INSERT INTO reply_pairs (inbound_text, reply_text, auto_feedback_processed) VALUES (?, ?, 0)",
            ("ext inbound", "Confirmed, see you at 2pm sharp."),
        )
        # personal: agent was way off (large edit).
        conn.execute(
            "INSERT INTO draft_events (inbound_text, generated_draft, sender_type, confidence) VALUES (?, ?, ?, ?)",
            ("per inbound", "Sounds good, I'll handle it.", "personal", "low"),
        )
        conn.execute(
            "INSERT INTO reply_pairs (inbound_text, reply_text, auto_feedback_processed) VALUES (?, ?, 0)",
            ("per inbound", "Actually no — let's cancel the whole thing and revisit next quarter when budgets reset."),
        )
        conn.commit()
        _capture_organic_pairs(conn, dry_run=False)
        conn.commit()
    finally:
        conn.close()

    summary = summarize_draft_events(url)
    by_sender = summary["outcome"]["avg_edit_distance_by_sender_type"]
    assert set(by_sender) >= {"external_client", "personal"}
    # personal was edited far more than external_client.
    assert by_sender["personal"]["avg_edit_distance"] > by_sender["external_client"]["avg_edit_distance"]

    weights = draft_quality_case_weights(summary)
    assert weights, "real captured distances must yield non-empty case weights"
    assert weights["personal"] > weights["external_client"], "weighting is no longer inert/uniform"


def test_non_organic_extract_path_stores_real_distance(tmp_path):
    """The main extract loop (generate-draft-then-compare) now persists a REAL
    edit_distance_pct instead of leaving it NULL — so its captured pairs survive
    the b185 honesty filter. Exercised at the SQL level using the same INSERT
    shape the loop emits (real distance, organic=0)."""
    url, db_path = _db_url(tmp_path)
    conn = _make_db(db_path)
    inbound = "What's the status on the migration?"
    draft = "It's on track — finishing the data backfill today."
    sent = "Slipping a day — the backfill hit a snag, expect it tomorrow EOD."
    real_ed = round(1.0 - similarity_ratio(draft, sent), 4)
    try:
        conn.execute(
            "INSERT INTO draft_events (inbound_text, generated_draft, sender_type, confidence) VALUES (?, ?, ?, ?)",
            (inbound, draft, "internal", "high"),
        )
        conn.execute(
            "INSERT INTO feedback_pairs "
            "(inbound_text, generated_draft, edited_reply, feedback_note, edit_distance_pct, rating, used_in_finetune, organic) "
            "VALUES (?, ?, ?, ?, ?, 4, 0, 0)",
            (inbound, draft, sent, "auto-captured from sent email", real_ed),
        )
        conn.commit()
    finally:
        conn.close()

    outcome = summarize_draft_events(url)["outcome"]
    assert outcome["matched"] == 1
    assert outcome["avg_edit_distance_by_sender_type"]["internal"]["avg_edit_distance"] == pytest.approx(
        round(real_ed, 3), abs=1e-3
    )
