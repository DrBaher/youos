"""Tests for youos corpus CLI command (Item 9)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.report_ingestion_health import corpus_report


def _create_test_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
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
            embedding BLOB,
            UNIQUE(source_type, source_id)
        );
        CREATE TABLE reply_pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL DEFAULT 'gmail',
            source_id TEXT NOT NULL,
            inbound_text TEXT NOT NULL,
            reply_text TEXT NOT NULL,
            quality_score REAL DEFAULT 1.0,
            UNIQUE(source_type, source_id)
        );
        CREATE TABLE feedback_pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inbound_text TEXT NOT NULL,
            generated_draft TEXT NOT NULL,
            edited_reply TEXT NOT NULL,
            rating INTEGER,
            used_in_finetune INTEGER DEFAULT 0,
            edit_distance_pct REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE sender_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            display_name TEXT,
            reply_count INTEGER DEFAULT 0
        );
    """)

    # Insert test data
    for i in range(10):
        conn.execute(
            "INSERT INTO documents (source_type, source_id, content) VALUES (?, ?, ?)",
            ("gmail", f"doc-{i}", f"content {i}"),
        )
    for i in range(5):
        conn.execute(
            "INSERT INTO reply_pairs (source_type, source_id, inbound_text, reply_text, quality_score) VALUES (?, ?, ?, ?, ?)",
            ("gmail", f"rp-{i}", f"inbound {i}", f"reply {i}", 0.8 + i * 0.05),
        )
    conn.execute(
        "INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply) VALUES (?, ?, ?)",
        ("test", "draft", "edited"),
    )
    conn.execute(
        "INSERT INTO sender_profiles (email, display_name, reply_count) VALUES (?, ?, ?)",
        ("alice@example.com", "Alice", 10),
    )
    conn.commit()
    conn.close()
    return db_path


def test_corpus_report_basic(tmp_path):
    db_path = _create_test_db(tmp_path)
    report = corpus_report(db_path)

    assert report["pair_count"] == 5
    assert report["doc_count"] == 10
    assert report["feedback_pairs"] == 1
    assert report["embedding_pct"] == 0.0


def test_corpus_report_quality_scores(tmp_path):
    db_path = _create_test_db(tmp_path)
    report = corpus_report(db_path)

    qs = report["quality_score"]
    assert qs["min"] is not None
    assert qs["median"] is not None
    assert qs["max"] is not None
    assert qs["min"] <= qs["median"] <= qs["max"]


def test_corpus_report_top_senders(tmp_path):
    db_path = _create_test_db(tmp_path)
    report = corpus_report(db_path)

    assert len(report["top_senders"]) >= 1
    assert report["top_senders"][0]["email"] == "alice@example.com"
    assert report["top_senders"][0]["reply_count"] == 10


def test_corpus_report_returns_dict(tmp_path):
    db_path = _create_test_db(tmp_path)
    report = corpus_report(db_path)
    assert isinstance(report, dict)
    assert "pair_count" in report
    assert "doc_count" in report
    assert "feedback_pairs" in report
    assert "embedding_pct" in report
    assert "quality_score" in report
    assert "top_senders" in report
