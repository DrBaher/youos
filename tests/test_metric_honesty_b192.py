"""Metric honesty (b192): NULL sender_type is "unlogged", not "unknown".

Background (b190 diagnosis): ~3756 historical ``draft_events`` rows were written
before the drafting path logged a sender at all, so they have
``sender_type IS NULL``. The stats summarizer used to fold NULL into the
"unknown" bucket via ``COALESCE(sender_type, 'unknown')``, which made the
dashboard report a scary ~78% "unknown" senders — implying a classification
failure that does not exist. ``classify_sender`` returns "unknown" ONLY for a
sender string with no parseable email (the no-email case), never for a
parseable address.

This is the same family of bug as the b185 hollow-metric fix: do not conflate
"we never recorded a sender" with "we classified the sender as unknown".

The fix is reporting-only:
  * NULL sender_type now buckets under "unlogged" (never recorded).
  * A genuine classifier "unknown" value stays "unknown" and is NOT merged
    with "unlogged".
  * No change to classify_sender, drafting, or gating.
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
            (
                e.get("inbound", "q"),
                e.get("draft", "d"),
                e.get("sender_type"),
                e.get("intent"),
                e.get("confidence"),
                e.get("length_flag"),
            ),
        )
    if feedback is not None:
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
            conn.execute(
                "INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply, edit_distance_pct, organic) "
                "VALUES (?, ?, ?, ?, 0)",
                (f["inbound"], f.get("draft", "agent-draft"), f.get("edited", "user-sent-reply"), f["edit"]),
            )
    conn.commit()
    conn.close()
    return f"sqlite:///{db}"


def test_null_sender_type_buckets_as_unlogged_not_unknown(tmp_path):
    # Historical sender-less rows (sender_type IS NULL) must NOT show as the
    # scary "unknown" classifier bucket; they are honestly "unlogged".
    url = _make_db(
        tmp_path,
        [
            {"sender_type": None},
            {"sender_type": None},
            {"sender_type": None},
        ],
    )
    s = summarize_draft_events(url)
    assert s["by_sender_type"] == {"unlogged": 3}
    assert "unknown" not in s["by_sender_type"]


def test_real_sender_types_keep_their_buckets(tmp_path):
    # Rows that DID log a sender keep their concrete classification buckets.
    url = _make_db(
        tmp_path,
        [
            {"sender_type": "internal"},
            {"sender_type": "internal"},
            {"sender_type": "external_client"},
            {"sender_type": "personal"},
            {"sender_type": "automated"},
        ],
    )
    s = summarize_draft_events(url)
    assert s["by_sender_type"] == {
        "internal": 2,
        "external_client": 1,
        "personal": 1,
        "automated": 1,
    }


def test_classifier_unknown_stays_unknown_and_not_merged_with_unlogged(tmp_path):
    # A genuine classifier "unknown" (a sender string with no parseable email)
    # is a REAL value in the column. It must stay "unknown" and stay SEPARATE
    # from the NULL/"unlogged" sender-less rows.
    url = _make_db(
        tmp_path,
        [
            {"sender_type": "unknown"},  # real classifier verdict (unparseable)
            {"sender_type": "unknown"},
            {"sender_type": None},  # never logged a sender
            {"sender_type": "internal"},
        ],
    )
    s = summarize_draft_events(url)
    assert s["by_sender_type"]["unknown"] == 2
    assert s["by_sender_type"]["unlogged"] == 1
    assert s["by_sender_type"]["internal"] == 1
    # The two are distinct keys — never folded together.
    assert "unknown" in s["by_sender_type"] and "unlogged" in s["by_sender_type"]


def test_sender_type_bucket_counts_sum_to_total(tmp_path):
    # The honest split must still account for every row.
    url = _make_db(
        tmp_path,
        [
            {"sender_type": None},
            {"sender_type": None},
            {"sender_type": "unknown"},
            {"sender_type": "internal"},
            {"sender_type": "external_client"},
        ],
    )
    s = summarize_draft_events(url)
    assert s["total"] == 5
    assert sum(s["by_sender_type"].values()) == s["total"]


def test_outcome_null_sender_type_buckets_as_unlogged(tmp_path):
    # The outcome correlation (edit-distance by sender_type) must apply the same
    # honest bucketing: a matched row with NULL sender_type is "unlogged".
    url = _make_db(
        tmp_path,
        [
            {"inbound": "A", "draft": "draft-X", "sender_type": None, "confidence": "high"},
            {"inbound": "B", "draft": "draft-Y", "sender_type": "internal", "confidence": "high"},
        ],
        feedback=[
            {"inbound": "A", "draft": "organic-X", "edit": 0.2},
            {"inbound": "B", "draft": "organic-Y", "edit": 0.4},
        ],
    )
    s = summarize_draft_events(url)
    by_sender = s["outcome"]["avg_edit_distance_by_sender_type"]
    assert by_sender["unlogged"]["avg_edit_distance"] == 0.2
    assert by_sender["internal"]["avg_edit_distance"] == 0.4
    assert "unknown" not in by_sender
