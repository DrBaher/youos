"""Draft-events analysis (consume the signal log soundly).

`summarize_draft_events` turns the per-draft signal log into a
draft-quality-by-condition picture (counts by intent/sender_type/confidence/
length_flag, off-target rate, and a best-effort edit-distance correlation).
This is analysis/observability for the loop — the model's drafts are never
training targets.
"""

from __future__ import annotations

import sqlite3

from app.core.stats import summarize_draft_events
from app.db.bootstrap import _migrate_draft_events


def _make_db(tmp_path, events, feedback=None) -> str:
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    _migrate_draft_events(conn)
    for e in events:
        conn.execute(
            "INSERT INTO draft_events (inbound_text, generated_draft, sender_type, intent, confidence, length_flag) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (e.get("inbound", "q"), e.get("draft", "d"), e.get("sender_type"), e.get("intent"), e.get("confidence"), e.get("length_flag")),
        )
    if feedback is not None:
        # b167: the real edit-distance ground truth is feedback_pairs, joined to
        # draft_events on inbound_text alone (the draft text differs between the
        # two tables on a live instance). Note generated_draft is intentionally a
        # different value from the draft_events draft, to mirror production.
        conn.execute(
            """CREATE TABLE feedback_pairs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, inbound_text TEXT NOT NULL,
                generated_draft TEXT NOT NULL, edited_reply TEXT NOT NULL,
                feedback_note TEXT, rating INTEGER, used_in_finetune INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP, edit_distance_pct REAL,
                reply_pair_id INTEGER, organic BOOLEAN DEFAULT 0,
                edit_categories TEXT, precedents_used TEXT
            )"""
        )
        for f in feedback:
            # b185: real draft-vs-sent rows (organic=0, draft<>edited) so they
            # survive the honesty filter; the join still keys on inbound_text.
            conn.execute(
                "INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply, edit_distance_pct, organic) "
                "VALUES (?, ?, ?, ?, 0)",
                (f["inbound"], f.get("draft", "agent-draft"), f.get("edited", "user-sent-reply"), f["edit"]),
            )
    conn.commit()
    conn.close()
    return f"sqlite:///{db}"


def test_counts_by_condition(tmp_path):
    url = _make_db(
        tmp_path,
        [
            {"intent": "scheduling", "sender_type": "internal", "confidence": "high", "length_flag": "ok"},
            {"intent": "scheduling", "sender_type": "internal", "confidence": "high", "length_flag": "long"},
            {"intent": "intro", "sender_type": "external_client", "confidence": "low", "length_flag": "short"},
        ],
    )
    s = summarize_draft_events(url)
    assert s["total"] == 3
    assert s["by_intent"] == {"scheduling": 2, "intro": 1}
    assert s["by_sender_type"] == {"internal": 2, "external_client": 1}
    assert s["by_confidence"] == {"high": 2, "low": 1}
    assert s["by_length_flag"] == {"ok": 1, "long": 1, "short": 1}
    assert s["off_target_pct"] == round(100 * 2 / 3, 1)  # long + short of 3 flagged


def test_off_target_excludes_null_flag(tmp_path):
    url = _make_db(
        tmp_path,
        [
            {"length_flag": "ok"},
            {"length_flag": "ok"},
            {"length_flag": "long"},
            {"length_flag": None},  # not length-annotated -> excluded from rate
        ],
    )
    s = summarize_draft_events(url)
    assert s["by_length_flag"] == {"ok": 2, "long": 1, "none": 1}
    assert s["off_target_pct"] == round(100 * 1 / 3, 1)  # 1 long of 3 flagged


def test_outcome_correlation_joins_to_edit_distance(tmp_path):
    # b167: join draft_events -> feedback_pairs on inbound_text. The draft texts
    # deliberately differ between the tables to mirror the live DB (where an
    # inbound+draft join matched ~nothing, the original always-zero bug).
    url = _make_db(
        tmp_path,
        [
            {"inbound": "A", "draft": "draft-X", "sender_type": "internal", "confidence": "high"},
            {"inbound": "B", "draft": "draft-Y", "sender_type": "external_client", "confidence": "low"},
        ],
        feedback=[
            {"inbound": "A", "draft": "organic-X", "edit": 0.1},
            {"inbound": "B", "draft": "organic-Y", "edit": 0.5},
        ],
    )
    s = summarize_draft_events(url)
    assert s["outcome"]["matched"] == 2
    by_sender = s["outcome"]["avg_edit_distance_by_sender_type"]
    assert by_sender["internal"]["avg_edit_distance"] == 0.1
    assert by_sender["external_client"]["avg_edit_distance"] == 0.5
    assert by_sender["internal"]["n"] == 1


def test_outcome_fanout_collapsed_per_inbound(tmp_path):
    # Many feedback rows per inbound are averaged FIRST (one mean per inbound),
    # so the cohort count is by draft_event, not by feedback row (b167 fan-out).
    url = _make_db(
        tmp_path,
        [{"inbound": "A", "draft": "d", "sender_type": "internal", "confidence": "high"}],
        feedback=[
            {"inbound": "A", "draft": "r1", "edit": 0.2},
            {"inbound": "A", "draft": "r2", "edit": 0.4},
        ],
    )
    s = summarize_draft_events(url)
    assert s["outcome"]["matched"] == 1  # one draft_event, not two
    assert s["outcome"]["avg_edit_distance_by_sender_type"]["internal"]["avg_edit_distance"] == 0.3
    assert s["outcome"]["avg_edit_distance_by_sender_type"]["internal"]["n"] == 1


def test_outcome_empty_without_feedback(tmp_path):
    url = _make_db(tmp_path, [{"sender_type": "internal"}])  # no feedback_pairs table
    s = summarize_draft_events(url)
    assert s["total"] == 1
    assert s["outcome"]["matched"] == 0
    assert s["outcome"]["avg_edit_distance_by_sender_type"] == {}


def test_missing_table_returns_zeroed_summary(tmp_path):
    db = tmp_path / "bare.db"
    sqlite3.connect(db).close()  # exists but no draft_events table
    s = summarize_draft_events(f"sqlite:///{db}")
    assert s["total"] == 0
    assert s["by_intent"] == {}
    assert s["off_target_pct"] is None
    assert s["outcome"]["matched"] == 0


def test_missing_db_returns_zeroed_summary(tmp_path):
    s = summarize_draft_events(f"sqlite:///{tmp_path / 'nope.db'}")
    assert s["total"] == 0
