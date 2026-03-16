"""Tests for organic pair capture from sent mail (Item 5)."""

import sqlite3

from scripts.extract_auto_feedback import _capture_organic_pairs


def _create_test_db(tmp_path):
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE reply_pairs (
            id INTEGER PRIMARY KEY,
            inbound_text TEXT,
            reply_text TEXT,
            inbound_author TEXT,
            source_type TEXT DEFAULT 'email',
            source_id TEXT DEFAULT '',
            document_id INTEGER,
            paired_at TEXT,
            created_ts TEXT DEFAULT CURRENT_TIMESTAMP,
            auto_feedback_processed INTEGER DEFAULT 0,
            quality_score REAL DEFAULT 1.0,
            thread_id TEXT,
            reply_author TEXT,
            metadata_json TEXT DEFAULT '{}'
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
            organic BOOLEAN DEFAULT 0
        )"""
    )
    return db, conn


def test_capture_organic_pairs(tmp_path):
    db, conn = _create_test_db(tmp_path)
    # Add reply pairs — one organic (no draft), one already processed
    conn.execute(
        "INSERT INTO reply_pairs (inbound_text, reply_text, auto_feedback_processed) VALUES (?, ?, ?)",
        ("Hey, can we meet?", "Sure, how about Tuesday at 3pm?", 0),
    )
    conn.execute(
        "INSERT INTO reply_pairs (inbound_text, reply_text, auto_feedback_processed) VALUES (?, ?, ?)",
        ("Thanks!", "No problem!", 1),
    )
    conn.commit()

    count = _capture_organic_pairs(conn, dry_run=False)
    conn.commit()
    assert count == 1

    rows = conn.execute("SELECT * FROM feedback_pairs WHERE organic = 1").fetchall()
    assert len(rows) == 1
    conn.close()


def test_organic_skips_short_replies(tmp_path):
    db, conn = _create_test_db(tmp_path)
    conn.execute(
        "INSERT INTO reply_pairs (inbound_text, reply_text, auto_feedback_processed) VALUES (?, ?, ?)",
        ("Hey", "Ok", 0),  # reply_text < 15 chars
    )
    conn.commit()

    count = _capture_organic_pairs(conn, dry_run=False)
    assert count == 0
    conn.close()


def test_organic_skips_already_captured(tmp_path):
    db, conn = _create_test_db(tmp_path)
    conn.execute(
        "INSERT INTO reply_pairs (inbound_text, reply_text, auto_feedback_processed) VALUES (?, ?, ?)",
        ("Hey, can we meet?", "Sure, how about Tuesday at 3pm?", 0),
    )
    # Already has a feedback_pair linked
    conn.execute(
        "INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply, reply_pair_id, organic) VALUES (?, ?, ?, ?, ?)",
        ("Hey, can we meet?", "draft", "reply", 1, 0),
    )
    conn.commit()

    count = _capture_organic_pairs(conn, dry_run=False)
    assert count == 0
    conn.close()


def test_organic_dry_run(tmp_path):
    db, conn = _create_test_db(tmp_path)
    conn.execute(
        "INSERT INTO reply_pairs (inbound_text, reply_text, auto_feedback_processed) VALUES (?, ?, ?)",
        ("Hey, can we meet?", "Sure, how about Tuesday at 3pm?", 0),
    )
    conn.commit()

    count = _capture_organic_pairs(conn, dry_run=True)
    assert count == 1

    # Should not have inserted anything
    rows = conn.execute("SELECT * FROM feedback_pairs").fetchall()
    assert len(rows) == 0
    conn.close()


def test_organic_column_migration(tmp_path):
    """If organic column is missing, it should be added."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE reply_pairs (
            id INTEGER PRIMARY KEY, inbound_text TEXT, reply_text TEXT,
            auto_feedback_processed INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE feedback_pairs (
            id INTEGER PRIMARY KEY, inbound_text TEXT, generated_draft TEXT,
            edited_reply TEXT, reply_pair_id INTEGER, used_in_finetune INTEGER DEFAULT 0
        )"""
    )
    conn.row_factory = sqlite3.Row
    conn.commit()

    # Should not raise
    count = _capture_organic_pairs(conn, dry_run=False)
    assert count == 0
    # Verify organic column was added
    cols = {row[1] for row in conn.execute("PRAGMA table_info(feedback_pairs)").fetchall()}
    assert "organic" in cols
    conn.close()
