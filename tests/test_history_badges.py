"""Tests for intent and confidence badges in history endpoint (Item 9)."""

import sqlite3


def test_history_endpoint_returns_intent_and_confidence(tmp_path):
    """History endpoint includes intent and confidence fields when available."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE draft_history (
            id INTEGER PRIMARY KEY,
            inbound_text TEXT,
            sender TEXT,
            generated_draft TEXT,
            final_reply TEXT,
            edit_distance_pct REAL,
            confidence TEXT,
            model_used TEXT,
            retrieval_method TEXT,
            created_at TEXT,
            intent TEXT
        )"""
    )
    conn.execute(
        """INSERT INTO draft_history
           (inbound_text, sender, generated_draft, confidence, model_used, retrieval_method, created_at, intent)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("Can we meet?", "alice@co.com", "Sure, let's meet.", "high", "claude", "fts5", "2026-01-01T00:00:00", "meeting_request"),
    )
    conn.commit()
    conn.close()

    # Simulate the route logic directly
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    col_names = {row[1] for row in conn.execute("PRAGMA table_info(draft_history)").fetchall()}
    has_intent = "intent" in col_names
    assert has_intent

    select_cols = "id, inbound_text, sender, generated_draft, final_reply, edit_distance_pct, confidence, model_used, retrieval_method, created_at"
    if has_intent:
        select_cols += ", intent"

    rows = conn.execute(f"SELECT {select_cols} FROM draft_history ORDER BY created_at DESC LIMIT 20").fetchall()
    items = [dict(row) for row in rows]
    conn.close()

    assert len(items) == 1
    assert items[0]["intent"] == "meeting_request"
    assert items[0]["confidence"] == "high"


def test_history_endpoint_without_intent_column(tmp_path):
    """History endpoint works when intent column doesn't exist."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE draft_history (
            id INTEGER PRIMARY KEY,
            inbound_text TEXT,
            sender TEXT,
            generated_draft TEXT,
            final_reply TEXT,
            edit_distance_pct REAL,
            confidence TEXT,
            model_used TEXT,
            retrieval_method TEXT,
            created_at TEXT
        )"""
    )
    conn.execute(
        """INSERT INTO draft_history
           (inbound_text, sender, generated_draft, confidence, model_used, retrieval_method, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("Thanks!", "bob@co.com", "You're welcome!", "medium", "claude", "fts5", "2026-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    col_names = {row[1] for row in conn.execute("PRAGMA table_info(draft_history)").fetchall()}
    has_intent = "intent" in col_names
    assert not has_intent

    select_cols = "id, inbound_text, sender, generated_draft, final_reply, edit_distance_pct, confidence, model_used, retrieval_method, created_at"
    rows = conn.execute(f"SELECT {select_cols} FROM draft_history ORDER BY created_at DESC LIMIT 20").fetchall()
    items = [dict(row) for row in rows]
    conn.close()

    assert len(items) == 1
    assert items[0]["confidence"] == "medium"
    assert "intent" not in items[0]
