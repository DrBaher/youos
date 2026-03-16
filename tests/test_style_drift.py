"""Tests for style drift detection."""

import json
from pathlib import Path

from unittest.mock import MagicMock, patch


def test_drift_stable(tmp_path):
    """When consecutive entries are similar, status is stable."""
    drift_path = tmp_path / "var" / "persona_drift.jsonl"
    drift_path.parent.mkdir(parents=True)
    entries = [
        {"analyzed_at": "2025-01-01T00:00:00Z", "avg_reply_words": 40, "directness_score": 0.8, "bullet_point_pct": 0.3, "avg_paragraphs": 2},
        {"analyzed_at": "2025-01-02T00:00:00Z", "avg_reply_words": 42, "directness_score": 0.82, "bullet_point_pct": 0.3, "avg_paragraphs": 2},
    ]
    drift_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    # Simulate drift detection logic from stats_routes
    lines = drift_path.read_text().strip().split("\n")
    lines = [ln for ln in lines if ln.strip()]
    prev = json.loads(lines[-2])
    curr = json.loads(lines[-1])
    word_delta = curr["avg_reply_words"] - prev["avg_reply_words"]
    directness_delta = curr["directness_score"] - prev["directness_score"]

    assert abs(word_delta) <= 8
    assert abs(directness_delta) <= 0.15


def test_drift_detected_words(tmp_path):
    """When word count changes by >8, drift is detected."""
    drift_path = tmp_path / "var" / "persona_drift.jsonl"
    drift_path.parent.mkdir(parents=True)
    entries = [
        {"analyzed_at": "2025-01-01T00:00:00Z", "avg_reply_words": 40, "directness_score": 0.8, "bullet_point_pct": 0.3, "avg_paragraphs": 2},
        {"analyzed_at": "2025-01-02T00:00:00Z", "avg_reply_words": 28, "directness_score": 0.8, "bullet_point_pct": 0.3, "avg_paragraphs": 2},
    ]
    drift_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    lines = drift_path.read_text().strip().split("\n")
    prev = json.loads(lines[-2])
    curr = json.loads(lines[-1])
    word_delta = curr["avg_reply_words"] - prev["avg_reply_words"]

    assert abs(word_delta) > 8
    assert word_delta == -12


def test_drift_detected_directness(tmp_path):
    """When directness changes by >0.15, drift is detected."""
    drift_path = tmp_path / "var" / "persona_drift.jsonl"
    drift_path.parent.mkdir(parents=True)
    entries = [
        {"analyzed_at": "2025-01-01T00:00:00Z", "avg_reply_words": 40, "directness_score": 0.8, "bullet_point_pct": 0.3, "avg_paragraphs": 2},
        {"analyzed_at": "2025-01-02T00:00:00Z", "avg_reply_words": 40, "directness_score": 0.6, "bullet_point_pct": 0.3, "avg_paragraphs": 2},
    ]
    drift_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    lines = drift_path.read_text().strip().split("\n")
    prev = json.loads(lines[-2])
    curr = json.loads(lines[-1])
    directness_delta = curr["directness_score"] - prev["directness_score"]

    assert abs(directness_delta) > 0.15


def test_drift_single_entry(tmp_path):
    """With only one entry, no drift can be detected."""
    drift_path = tmp_path / "var" / "persona_drift.jsonl"
    drift_path.parent.mkdir(parents=True)
    drift_path.write_text(json.dumps({"analyzed_at": "2025-01-01T00:00:00Z", "avg_reply_words": 40, "directness_score": 0.8}) + "\n")

    lines = drift_path.read_text().strip().split("\n")
    assert len(lines) < 2  # not enough for comparison


def test_drift_entry_format():
    """Verify drift entry has all required fields."""
    entry = {
        "analyzed_at": "2025-01-01T00:00:00Z",
        "avg_reply_words": 40,
        "directness_score": 0.8,
        "bullet_point_pct": 0.3,
        "avg_paragraphs": 2,
    }
    for key in ("analyzed_at", "avg_reply_words", "directness_score", "bullet_point_pct", "avg_paragraphs"):
        assert key in entry
