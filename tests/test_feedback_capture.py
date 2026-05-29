"""Tests for queue-lifecycle feedback capture (Phase C).

Mines the agent's own terminal queue rows into feedback_pairs: edited→kept
(correction), sent-unchanged (positive), dismissed-wrong_content (negative).
Idempotent via the feedback_captured marker.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.agent.feedback_capture import _classify_row, capture_queue_feedback
from app.db.bootstrap import _migrate_agent_pending_drafts, _migrate_feedback_pairs


@pytest.fixture
def db(tmp_path):
    """A DB with agent_pending_drafts + feedback_pairs. Returns (url, insert)."""
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    # feedback_pairs base table (matches docs/schema.sql) + migrated columns.
    conn.execute(
        "CREATE TABLE feedback_pairs ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, inbound_text TEXT NOT NULL, "
        " generated_draft TEXT NOT NULL, edited_reply TEXT NOT NULL, "
        " feedback_note TEXT, rating INTEGER, used_in_finetune INTEGER DEFAULT 0, "
        " edit_distance_pct REAL, reply_pair_id INTEGER, "
        " created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    _migrate_feedback_pairs(conn)
    _migrate_agent_pending_drafts(conn)
    conn.commit()
    n = {"i": 0}

    def insert(**over):
        n["i"] += 1
        i = n["i"]
        cols = {
            "message_id": f"m{i}", "thread_id": f"t{i}", "account": "me@x.com",
            "sender_email": "a@x.com", "subject": "Q", "body": "Please confirm the plan?",
            "needs_reply_score": 0.9, "reasons_json": "[]", "cold_outreach": 0,
            "tier": "draft", "draft": "Yes, the plan works for me.",
            "status": "pending", "amended_draft": None, "send_state": None,
            "dismissal_reason": None,
        }
        cols.update(over)
        keys = ", ".join(cols)
        qs = ", ".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO agent_pending_drafts ({keys}) VALUES ({qs})",
            list(cols.values()),
        )
        conn.commit()
        return i

    return f"sqlite:///{path}", insert, conn


# --- _classify_row ---------------------------------------------------------


def test_classify_edited_then_kept_is_correction():
    pair = _classify_row({
        "draft": "Yes, sounds good.", "body": "Can you confirm?",
        "amended_draft": "Yes — confirmed, see you at 3pm sharp.",
        "status": "amended", "send_state": None, "dismissal_reason": None,
    })
    assert pair["rating"] == 4
    assert pair["edited"] == "Yes — confirmed, see you at 3pm sharp."
    assert pair["edit_distance_pct"] > 0


def test_classify_sent_unchanged_is_positive():
    pair = _classify_row({
        "draft": "Yes, sounds good.", "body": "Can you confirm?",
        "amended_draft": None, "status": "sent", "send_state": "sent",
        "dismissal_reason": None,
    })
    assert pair["rating"] == 5
    assert pair["edited"] == pair["generated"]


def test_classify_machine_regenerate_amend_is_not_a_correction():
    """A /regenerate re-draft (amended_by='machine') must NOT be mined as a gold
    human correction — that would train the model on its own output."""
    assert _classify_row({
        "draft": "Yes, sounds good.", "body": "Can you confirm?",
        "amended_draft": "Yes — confirmed, see you at 3pm.", "amended_by": "machine",
        "status": "amended", "send_state": None, "dismissal_reason": None,
    }) is None


def test_classify_user_amend_still_a_correction():
    pair = _classify_row({
        "draft": "Yes, sounds good.", "body": "Can you confirm?",
        "amended_draft": "Yes — confirmed, see you at 3pm.", "amended_by": "user",
        "status": "amended", "send_state": None, "dismissal_reason": None,
    })
    assert pair is not None and pair["rating"] == 4


def test_classify_dismissed_wrong_content_is_negative():
    pair = _classify_row({
        "draft": "Yes, sounds good.", "body": "Can you confirm?",
        "amended_draft": None, "status": "dismissed",
        "send_state": None, "dismissal_reason": "wrong_content",
    })
    assert pair["rating"] == 2


def test_classify_noise_dismissal_is_skipped():
    assert _classify_row({
        "draft": "Yes.", "body": "buy now!", "amended_draft": None,
        "status": "dismissed", "send_state": None, "dismissal_reason": "noise",
    }) is None


def test_classify_surface_or_empty_skipped():
    assert _classify_row({"draft": None, "body": "x", "status": "sent"}) is None
    assert _classify_row({"draft": "y", "body": None, "status": "sent"}) is None


# --- capture_queue_feedback ------------------------------------------------


def test_capture_inserts_pairs_and_marks_rows(db):
    database_url, insert, conn = db
    insert(status="amended", amended_draft="A much better edited reply here.")
    insert(status="sent", send_state="sent")
    insert(status="dismissed", dismissal_reason="wrong_content")
    insert(status="dismissed", dismissal_reason="noise")        # skipped
    insert(status="pending")                                     # not terminal

    r = capture_queue_feedback(database_url)
    assert r["scanned"] == 4   # the pending row isn't scanned
    assert r["captured"] == 3
    assert r["skipped"] == 1

    fp = conn.execute("SELECT rating, feedback_note FROM feedback_pairs ORDER BY id").fetchall()
    assert {row[0] for row in fp} == {2, 4, 5}
    assert all("agent-queue" in row[1] for row in fp)


def test_capture_is_idempotent(db):
    database_url, insert, conn = db
    insert(status="sent", send_state="sent")
    first = capture_queue_feedback(database_url)
    second = capture_queue_feedback(database_url)
    assert first["captured"] == 1
    assert second["scanned"] == 0  # already marked
    assert conn.execute("SELECT COUNT(*) FROM feedback_pairs").fetchone()[0] == 1


def test_capture_pairs_feed_finetune_unused(db):
    database_url, insert, conn = db
    insert(status="amended", amended_draft="Edited and improved reply text.")
    capture_queue_feedback(database_url)
    # Captured with used_in_finetune=0 so the nightly fine-tune picks them up.
    n = conn.execute("SELECT COUNT(*) FROM feedback_pairs WHERE used_in_finetune = 0").fetchone()[0]
    assert n == 1
