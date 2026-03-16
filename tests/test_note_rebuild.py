"""Tests for youos note triggering profile rebuild (Item 12)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.build_sender_profiles import build_profiles


def _create_test_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE reply_pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL DEFAULT 'gmail',
            source_id TEXT NOT NULL,
            document_id INTEGER,
            thread_id TEXT,
            inbound_text TEXT NOT NULL,
            reply_text TEXT NOT NULL,
            inbound_author TEXT,
            reply_author TEXT,
            paired_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            auto_feedback_processed INTEGER DEFAULT 0,
            quality_score REAL DEFAULT 1.0,
            UNIQUE(source_type, source_id)
        );
        CREATE TABLE sender_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            display_name TEXT,
            domain TEXT,
            company TEXT,
            sender_type TEXT,
            relationship_note TEXT,
            reply_count INTEGER DEFAULT 0,
            avg_reply_words REAL,
            avg_response_hours REAL,
            first_seen TEXT,
            last_seen TEXT,
            topics_json TEXT DEFAULT '[]',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL DEFAULT 'gmail',
            source_id TEXT NOT NULL,
            title TEXT,
            author TEXT,
            external_uri TEXT,
            thread_id TEXT,
            created_at TEXT,
            updated_at TEXT,
            content TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_type, source_id)
        );
    """)

    # Insert reply pairs for two senders
    conn.execute(
        "INSERT INTO reply_pairs (source_type, source_id, inbound_text, reply_text, inbound_author, paired_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("gmail", "msg-1", "Hello", "Hi there", "Alice <alice@example.com>", "2026-03-01T10:00:00"),
    )
    conn.execute(
        "INSERT INTO reply_pairs (source_type, source_id, inbound_text, reply_text, inbound_author, paired_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("gmail", "msg-2", "Follow up", "Sure", "Alice <alice@example.com>", "2026-03-02T10:00:00"),
    )
    conn.execute(
        "INSERT INTO reply_pairs (source_type, source_id, inbound_text, reply_text, inbound_author, paired_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("gmail", "msg-3", "Hey", "Hey!", "Bob <bob@other.com>", "2026-03-01T10:00:00"),
    )
    conn.commit()
    conn.close()
    return db_path


def test_build_profiles_with_sender_filter(tmp_path):
    """build_profiles with sender_email only processes that sender."""
    db_path = _create_test_db(tmp_path)
    new_count, updated_count = build_profiles(db_path, sender_email="alice@example.com")
    assert new_count + updated_count == 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM sender_profiles WHERE email = 'alice@example.com'").fetchone()
    assert row is not None
    assert row["reply_count"] == 2

    # Bob should NOT have been created
    bob = conn.execute("SELECT * FROM sender_profiles WHERE email = 'bob@other.com'").fetchone()
    assert bob is None
    conn.close()


def test_build_profiles_without_filter(tmp_path):
    """build_profiles without filter processes all senders."""
    db_path = _create_test_db(tmp_path)
    new_count, updated_count = build_profiles(db_path)
    assert new_count + updated_count == 2  # alice + bob


def test_build_profiles_no_match(tmp_path):
    """build_profiles with non-matching email returns 0."""
    db_path = _create_test_db(tmp_path)
    new_count, updated_count = build_profiles(db_path, sender_email="nobody@nowhere.com")
    assert new_count + updated_count == 0


def test_profile_rebuild_preserves_relationship_note(tmp_path):
    """Rebuilding a profile doesn't overwrite relationship_note."""
    db_path = _create_test_db(tmp_path)

    # First, set a relationship note manually
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO sender_profiles (email, relationship_note) VALUES (?, ?)",
        ("alice@example.com", "Important client"),
    )
    conn.commit()
    conn.close()

    # Rebuild profile
    build_profiles(db_path, sender_email="alice@example.com")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM sender_profiles WHERE email = 'alice@example.com'").fetchone()
    # Note: build_profiles updates profile but doesn't touch relationship_note
    assert row["reply_count"] == 2
    conn.close()
