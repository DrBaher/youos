"""Tests for multi-intent classification and corpus-learned intent (Item 3)."""

import json
import sqlite3

from app.core.intent import classify_intents_multi


def test_multi_single_intent():
    result = classify_intents_multi("Can we schedule a meeting?")
    assert result[0] == "meeting_request"


def test_multi_returns_general_for_empty():
    assert classify_intents_multi("") == ["general"]


def test_multi_returns_general_for_no_match():
    assert classify_intents_multi("Hello there") == ["general"]


def test_multi_two_intents():
    text = "This is urgent — can we schedule a meeting ASAP?"
    result = classify_intents_multi(text)
    assert len(result) >= 2
    assert "urgent" in result
    assert "meeting_request" in result


def test_multi_max_three():
    text = "urgent meeting to approve the proposal and update status"
    result = classify_intents_multi(text)
    assert len(result) <= 3


def test_multi_sorted_by_score():
    text = "schedule a meeting meeting meeting, also urgent"
    result = classify_intents_multi(text)
    assert result[0] == "meeting_request"


def test_annotate_intents(tmp_path):
    """annotate_intents writes predicted_intent to metadata_json."""
    from scripts.build_sender_profiles import annotate_intents

    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE reply_pairs (
            id INTEGER PRIMARY KEY,
            inbound_text TEXT,
            reply_text TEXT,
            metadata_json TEXT,
            inbound_author TEXT,
            reply_author TEXT,
            source_type TEXT DEFAULT 'email',
            source_id TEXT DEFAULT '',
            document_id INTEGER,
            paired_at TEXT,
            created_ts TEXT DEFAULT CURRENT_TIMESTAMP,
            auto_feedback_processed INTEGER DEFAULT 0,
            quality_score REAL DEFAULT 1.0,
            thread_id TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO reply_pairs (inbound_text, reply_text, metadata_json) VALUES (?, ?, ?)",
        ("Can we schedule a meeting?", "Sure, how about Tuesday?", "{}"),
    )
    conn.execute(
        "INSERT INTO reply_pairs (inbound_text, reply_text, metadata_json) VALUES (?, ?, ?)",
        ("Thanks for your help", "You're welcome!", json.dumps({"predicted_intent": "thank_you"})),
    )
    conn.commit()
    conn.close()

    count = annotate_intents(db)
    assert count == 1  # Only the one without predicted_intent

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT metadata_json FROM reply_pairs WHERE id = 1").fetchone()
    meta = json.loads(row[0])
    assert meta["predicted_intent"] == "meeting_request"

    # The already-annotated one should be unchanged
    row2 = conn.execute("SELECT metadata_json FROM reply_pairs WHERE id = 2").fetchone()
    meta2 = json.loads(row2[0])
    assert meta2["predicted_intent"] == "thank_you"
    conn.close()


def test_retrieval_request_has_intent_hint_2():
    """RetrievalRequest should accept intent_hint_2."""
    from app.retrieval.service import RetrievalRequest

    req = RetrievalRequest(query="test", intent_hint="urgent", intent_hint_2="meeting_request")
    assert req.intent_hint_2 == "meeting_request"
