"""Tests for unified stats query layer."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from app.core.stats import get_corpus_stats, get_model_status, get_pipeline_status


def test_get_corpus_stats_no_db(tmp_path):
    """Returns zeros when database doesn't exist."""
    result = get_corpus_stats(f"sqlite:///{tmp_path}/nonexistent.db")
    assert result["total_documents"] == 0
    assert result["total_reply_pairs"] == 0
    assert result["total_feedback_pairs"] == 0


def test_get_model_status_no_adapter():
    """Returns claude fallback when no adapter exists."""
    with patch("app.core.stats.ADAPTER_PATH", Path("/nonexistent/adapters")):
        result = get_model_status(Path("/tmp/configs"))
    assert result["generation_model"] == "claude"
    assert result["lora_adapter_exists"] is False
    assert result["lora_trained_at"] is None


def test_get_model_status_with_adapter(tmp_path):
    """Returns local model info when adapter exists."""
    adapter_dir = tmp_path / "adapters"
    adapter_dir.mkdir()
    (adapter_dir / "adapters.safetensors").write_text("fake")
    with patch("app.core.stats.ADAPTER_PATH", adapter_dir):
        result = get_model_status(Path("/tmp/configs"))
    assert result["generation_model"] == "qwen2.5-1.5b-lora"
    assert result["lora_adapter_exists"] is True
    assert result["lora_trained_at"] is not None


def test_get_pipeline_status_missing(tmp_path):
    """Returns None when no pipeline log exists."""
    result = get_pipeline_status(tmp_path)
    assert result is None


def test_get_pipeline_status_exists(tmp_path):
    """Returns parsed JSON when pipeline log exists."""
    var_dir = tmp_path / "var"
    var_dir.mkdir()
    log = {"run_at": "2026-03-16T01:00:00", "status": "ok", "steps": {}, "errors": []}
    (var_dir / "pipeline_last_run.json").write_text(json.dumps(log))
    result = get_pipeline_status(tmp_path)
    assert result["status"] == "ok"


def test_get_pipeline_status_corrupt(tmp_path):
    """Returns None when pipeline log is corrupt."""
    var_dir = tmp_path / "var"
    var_dir.mkdir()
    (var_dir / "pipeline_last_run.json").write_text("not json")
    result = get_pipeline_status(tmp_path)
    assert result is None


def test_get_corpus_stats_outcome_metrics(tmp_path):
    """Computes outcome metrics from feedback_pairs."""
    db_path = tmp_path / "stats.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY, embedding BLOB)")
        conn.execute(
            """
            CREATE TABLE feedback_pairs (
                id INTEGER PRIMARY KEY,
                created_at TEXT,
                edit_distance_pct REAL,
                rating INTEGER
            )
            """
        )

        conn.executemany("INSERT INTO documents(id) VALUES(?)", [(1,), (2,)])
        conn.executemany("INSERT INTO reply_pairs(id, embedding) VALUES(?, ?)", [(1, b"x"), (2, None), (3, b"y")])

        rows = [
            (1, "2026-03-17", 0.00, 5),
            (2, "2026-03-17", 0.05, 4),
            (3, "2026-03-16", 0.20, 3),
            (4, "2026-03-15", 0.40, 2),
        ]
        conn.executemany(
            "INSERT INTO feedback_pairs(id, created_at, edit_distance_pct, rating) VALUES(?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    result = get_corpus_stats(f"sqlite:///{db_path}")

    assert result["total_documents"] == 2
    assert result["total_reply_pairs"] == 3
    assert result["total_feedback_pairs"] == 4
    assert result["embedding_pct"] == 66.7

    outcome = result["outcome_metrics"]
    assert outcome["accept_unchanged_pct"] == 25.0
    assert outcome["low_edit_pct"] == 50.0
    assert outcome["high_rating_pct"] == 50.0
    assert outcome["median_edit_distance"] == 0.125
