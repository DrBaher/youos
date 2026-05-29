"""Tests for the real-mail triage precision harness (Phase A2).

Ground truth is the user's own verdicts on queued rows. These tests seed an
``agent_pending_drafts`` table with decided rows and check the confusion
matrix / precision / recall and the history snapshot.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.db.bootstrap import (
    _migrate_agent_pending_drafts,
    _migrate_triage_precision_history,
)
from app.evaluation.real_mail_eval import (
    _truth_label,
    evaluate_real_mail,
    precision_history,
    record_snapshot,
    run_and_record,
)


@pytest.fixture
def seeded_db(tmp_path):
    """A DB with decided queue rows. Returns (database_url, insert_fn)."""
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    _migrate_agent_pending_drafts(conn)
    _migrate_triage_precision_history(conn)
    conn.commit()

    seq = {"n": 0}

    def insert(tier, status, dismissal_reason=None, sender_email=None):
        seq["n"] += 1
        n = seq["n"]
        conn.execute(
            "INSERT INTO agent_pending_drafts "
            "(message_id, thread_id, account, sender_email, needs_reply_score, "
            " reasons_json, cold_outreach, tier, status, dismissal_reason) "
            "VALUES (?, ?, ?, ?, ?, '[]', 0, ?, ?, ?)",
            (f"m{n}", f"t{n}", "you@example.com", sender_email or f"s{n}@x.com",
             0.8, tier, status, dismissal_reason),
        )
        conn.commit()

    return f"sqlite:///{db}", insert


# --- _truth_label ----------------------------------------------------------


def test_truth_label_mapping():
    assert _truth_label("sent", None) == "pos"
    assert _truth_label("amended", None) == "pos"
    assert _truth_label("dismissed", "wrong_content") == "pos"
    assert _truth_label("dismissed", "noise") == "neg"
    assert _truth_label("dismissed", "wrong_sender") == "neg"
    # Ambiguous / undecided → excluded.
    assert _truth_label("dismissed", "already_handled") is None
    assert _truth_label("dismissed", "other") is None
    assert _truth_label("dismissed", None) is None
    assert _truth_label("pending", None) is None


# --- evaluate_real_mail ----------------------------------------------------


def test_confusion_matrix_and_metrics(seeded_db):
    database_url, insert = seeded_db
    # TP: drafted, then sent/amended.
    insert("draft", "sent")
    insert("draft", "amended")
    insert("draft", "dismissed", "wrong_content")  # deserved a reply (TP)
    # FP: drafted, dismissed as noise / wrong sender.
    insert("draft", "dismissed", "noise")
    insert("draft", "dismissed", "wrong_sender")
    # FN: surfaced (abstained) but the user ended up sending.
    insert("surface", "sent")
    # TN: surfaced, dismissed as noise.
    insert("surface", "dismissed", "noise")
    # Excluded: ambiguous + pending.
    insert("draft", "dismissed", "already_handled")
    insert("draft", "pending")

    r = evaluate_real_mail(database_url, days=365)
    assert r["confusion"] == {"tp": 3, "fp": 2, "fn": 1, "tn": 1}
    assert r["sample_size"] == 7
    assert r["excluded"] == 2
    # precision = 3/(3+2) = 0.6 ; recall = 3/(3+1) = 0.75
    assert r["precision"] == 0.6
    assert r["recall"] == 0.75
    assert r["fp_by_reason"] == {"noise": 1, "wrong_sender": 1}


def test_metrics_none_when_no_labelable_rows(seeded_db):
    database_url, insert = seeded_db
    insert("draft", "pending")
    insert("draft", "dismissed", "already_handled")
    r = evaluate_real_mail(database_url, days=365)
    assert r["precision"] is None
    assert r["recall"] is None
    assert r["f1"] is None
    assert r["sample_size"] == 0
    assert r["excluded"] == 2


def test_account_filter(seeded_db):
    database_url, insert = seeded_db
    insert("draft", "sent", sender_email="a@x.com")
    # A different account's rows shouldn't be counted.
    import sqlite3 as _s
    conn = _s.connect(database_url.removeprefix("sqlite:///"))
    conn.execute(
        "INSERT INTO agent_pending_drafts "
        "(message_id, thread_id, account, sender_email, needs_reply_score, "
        " reasons_json, cold_outreach, tier, status) "
        "VALUES ('z1','z1','other@x.com','z@x.com',0.8,'[]',0,'draft','sent')"
    )
    conn.commit()
    conn.close()
    r = evaluate_real_mail(database_url, account="you@example.com", days=365)
    assert r["confusion"]["tp"] == 1


# --- snapshots / history ---------------------------------------------------


def test_record_and_read_history(seeded_db):
    database_url, insert = seeded_db
    insert("draft", "sent")
    insert("draft", "dismissed", "noise")
    r = evaluate_real_mail(database_url, days=30)
    rid = record_snapshot(database_url, r, days=30)
    assert rid is not None

    hist = precision_history(database_url, limit=10)
    assert len(hist) == 1
    row = hist[0]
    assert row["tp"] == 1
    assert row["fp"] == 1
    assert row["precision"] == 0.5
    assert row["sample_size"] == 2


def test_run_and_record_roundtrip(seeded_db):
    database_url, insert = seeded_db
    insert("draft", "sent")
    r = run_and_record(database_url, days=30)
    assert r["confusion"]["tp"] == 1
    assert len(precision_history(database_url)) == 1
