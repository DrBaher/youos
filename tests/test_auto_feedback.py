"""Tests for auto-feedback similarity threshold auto-calibration (Item 1)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.extract_auto_feedback import auto_calibrate_threshold, extract_auto_feedback


def _create_test_db(tmp_path: Path, pair_count: int = 0) -> Path:
    """Create a minimal test database with the required schema."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS reply_pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL DEFAULT 'gmail',
            source_id TEXT NOT NULL,
            inbound_text TEXT NOT NULL,
            reply_text TEXT NOT NULL,
            inbound_author TEXT,
            reply_author TEXT,
            paired_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            auto_feedback_processed INTEGER DEFAULT 0,
            quality_score REAL DEFAULT 1.0,
            document_id INTEGER,
            thread_id TEXT,
            UNIQUE(source_type, source_id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS feedback_pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inbound_text TEXT NOT NULL,
            generated_draft TEXT NOT NULL,
            edited_reply TEXT NOT NULL,
            feedback_note TEXT,
            rating INTEGER,
            used_in_finetune INTEGER DEFAULT 0,
            edit_distance_pct REAL,
            reply_pair_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    for i in range(pair_count):
        conn.execute(
            "INSERT INTO reply_pairs (source_type, source_id, inbound_text, reply_text) VALUES (?, ?, ?, ?)",
            ("gmail", f"msg-{i}", f"inbound {i}", f"reply {i}"),
        )
    conn.commit()
    conn.close()
    return db_path


def test_auto_calibrate_small_corpus(tmp_path):
    """Corpus < 100 pairs should use threshold 0.65."""
    db_path = _create_test_db(tmp_path, pair_count=47)
    conn = sqlite3.connect(db_path)
    threshold, count = auto_calibrate_threshold(conn)
    conn.close()
    assert threshold == 0.65
    assert count == 47


def test_auto_calibrate_medium_corpus(tmp_path):
    """Corpus 100-499 pairs should use threshold 0.72."""
    db_path = _create_test_db(tmp_path, pair_count=250)
    conn = sqlite3.connect(db_path)
    threshold, count = auto_calibrate_threshold(conn)
    conn.close()
    assert threshold == 0.72
    assert count == 250


def test_auto_calibrate_large_corpus(tmp_path):
    """Corpus >= 500 pairs should use threshold 0.80."""
    db_path = _create_test_db(tmp_path, pair_count=500)
    conn = sqlite3.connect(db_path)
    threshold, count = auto_calibrate_threshold(conn)
    conn.close()
    assert threshold == 0.80
    assert count == 500


def test_extract_auto_feedback_with_auto_threshold(tmp_path):
    """extract_auto_feedback uses auto-calibrated threshold by default."""
    db_path = _create_test_db(tmp_path, pair_count=10)
    result = extract_auto_feedback(
        days=1,
        dry_run=True,
        db_path=db_path,
        auto_threshold=True,
        database_url=f"sqlite:///{db_path}",
        configs_dir=tmp_path,
    )
    assert "captured" in result
    assert "total" in result


def test_extract_auto_feedback_without_auto_threshold(tmp_path):
    """When auto_threshold is False, the explicit threshold value is used."""
    db_path = _create_test_db(tmp_path, pair_count=10)
    result = extract_auto_feedback(
        days=1,
        dry_run=True,
        db_path=db_path,
        threshold=0.90,
        auto_threshold=False,
        database_url=f"sqlite:///{db_path}",
        configs_dir=tmp_path,
    )
    assert "captured" in result
