"""Tests for smart pipeline skip gates (Item 7)."""

import sqlite3

from scripts.nightly_pipeline import (
    should_skip_autoresearch,
    should_skip_dedup,
    should_skip_embeddings,
    should_skip_finetune,
)


def _create_db(tmp_path, *, pairs=0, feedback=0, null_embeddings=0):
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE reply_pairs (
            id INTEGER PRIMARY KEY, inbound_text TEXT, reply_text TEXT,
            created_ts TEXT DEFAULT CURRENT_TIMESTAMP, auto_feedback_processed INTEGER DEFAULT 0,
            quality_score REAL DEFAULT 1.0
        )"""
    )
    conn.execute(
        """CREATE TABLE feedback_pairs (
            id INTEGER PRIMARY KEY, inbound_text TEXT, generated_draft TEXT,
            edited_reply TEXT, used_in_finetune INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        """CREATE TABLE chunks (
            id INTEGER PRIMARY KEY, document_id INTEGER, chunk_index INTEGER,
            content TEXT, embedding BLOB, metadata_json TEXT DEFAULT '{}'
        )"""
    )
    for i in range(pairs):
        conn.execute("INSERT INTO reply_pairs (inbound_text, reply_text) VALUES (?, ?)", (f"q{i}", f"a{i}"))
    for i in range(feedback):
        conn.execute(
            "INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply) VALUES (?, ?, ?)",
            (f"q{i}", f"d{i}", f"r{i}"),
        )
    for i in range(null_embeddings):
        conn.execute("INSERT INTO chunks (document_id, chunk_index, content, embedding) VALUES (?, ?, ?, ?)", (1, i, f"c{i}", None))
    conn.commit()
    conn.close()
    return db


def test_skip_finetune_when_few_pairs(tmp_path):
    db = _create_db(tmp_path, feedback=2)
    skip, msg = should_skip_finetune(db)
    assert skip is True
    assert "only" in msg and "need >= 3" in msg


def test_no_skip_finetune_enough_pairs(tmp_path):
    db = _create_db(tmp_path, feedback=5)
    skip, _ = should_skip_finetune(db)
    assert skip is False  # 5 >= 3, no skip


def test_skip_autoresearch_when_few_pairs(tmp_path):
    db = _create_db(tmp_path, feedback=3)
    skip, msg = should_skip_autoresearch(db)
    assert skip is True
    assert "need >= 5" in msg


def test_no_skip_autoresearch_enough_pairs(tmp_path):
    db = _create_db(tmp_path, feedback=10)
    skip, _ = should_skip_autoresearch(db)
    assert skip is False


def test_skip_embeddings_all_indexed(tmp_path):
    db = _create_db(tmp_path)
    skip, msg = should_skip_embeddings(db)
    assert skip is True
    assert "already indexed" in msg


def test_no_skip_embeddings_when_null(tmp_path):
    db = _create_db(tmp_path, null_embeddings=5)
    skip, _ = should_skip_embeddings(db)
    assert skip is False


def test_skip_dedup_small_corpus(tmp_path):
    db = _create_db(tmp_path, pairs=5)
    skip, msg = should_skip_dedup(db)
    assert skip is True
    assert "too small" in msg


def test_no_skip_dedup_enough_pairs(tmp_path):
    db = _create_db(tmp_path, pairs=15)
    skip, _ = should_skip_dedup(db)
    assert skip is False


def test_skip_with_nonexistent_db(tmp_path):
    db = tmp_path / "nonexistent.db"
    skip, _ = should_skip_finetune(db)
    assert skip is True
    skip, _ = should_skip_dedup(db)
    assert skip is True
