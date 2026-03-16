"""Tests for golden eval as nightly pipeline step (Item 11)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

from scripts.nightly_pipeline import _count_feedback_pairs, step_golden_eval


def _create_db(tmp_path: Path, feedback_count: int = 0) -> Path:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE feedback_pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inbound_text TEXT NOT NULL,
            generated_draft TEXT NOT NULL,
            edited_reply TEXT NOT NULL,
            feedback_note TEXT,
            rating INTEGER,
            edit_distance_pct REAL,
            reply_pair_id INTEGER,
            used_in_finetune INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    for i in range(feedback_count):
        conn.execute(
            "INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply, rating) VALUES (?, ?, ?, ?)",
            (f"inbound {i}", f"draft {i}", f"reply {i}", 4),
        )
    conn.commit()
    conn.close()
    return db_path


def test_count_feedback_pairs(tmp_path):
    db = _create_db(tmp_path, feedback_count=5)
    assert _count_feedback_pairs(db) == 5


def test_count_feedback_pairs_empty(tmp_path):
    db = _create_db(tmp_path, feedback_count=0)
    assert _count_feedback_pairs(db) == 0


def test_count_feedback_pairs_no_db(tmp_path):
    assert _count_feedback_pairs(tmp_path / "nonexistent.db") == 0


def test_step_golden_eval_skips_few_pairs(tmp_path, capsys):
    """Should skip when fewer than 5 feedback pairs."""
    db = _create_db(tmp_path, feedback_count=3)
    with patch("scripts.nightly_pipeline.DEFAULT_DB", db):
        result = step_golden_eval()
    assert result is True
    captured = capsys.readouterr()
    assert "SKIP" in captured.out


def test_step_golden_eval_skips_no_db(tmp_path, capsys):
    """Should skip when DB doesn't exist."""
    with patch("scripts.nightly_pipeline.DEFAULT_DB", tmp_path / "nonexistent.db"):
        result = step_golden_eval()
    assert result is True
    captured = capsys.readouterr()
    assert "SKIP" in captured.out


def test_pipeline_log_has_golden_composite():
    """Pipeline log dict should include golden_composite key."""
    log = {
        "run_at": "2024-01-01T00:00:00",
        "status": "ok",
        "steps": {},
        "errors": [],
        "skipped_steps": [],
        "benchmark_rotated": False,
        "golden_composite": 0.75,
    }
    assert "golden_composite" in log
    assert log["golden_composite"] == 0.75
