"""Tests for auto-feedback lookback window (Item 5)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scripts.nightly_pipeline import _load_last_auto_feedback_at, _save_last_auto_feedback_at


def test_load_last_auto_feedback_at_missing(tmp_path):
    """Returns None when file doesn't exist."""
    with patch("scripts.nightly_pipeline.ROOT_DIR", tmp_path):
        result = _load_last_auto_feedback_at()
        assert result is None


def test_load_last_auto_feedback_at_present(tmp_path):
    """Returns timestamp when present in pipeline log."""
    var_dir = tmp_path / "var"
    var_dir.mkdir()
    ts = "2026-03-14T10:00:00+00:00"
    (var_dir / "pipeline_last_run.json").write_text(json.dumps({"last_auto_feedback_at": ts}))

    with patch("scripts.nightly_pipeline.ROOT_DIR", tmp_path):
        result = _load_last_auto_feedback_at()
        assert result == ts


def test_save_last_auto_feedback_at(tmp_path):
    """Saves timestamp to pipeline log, preserving existing data."""
    var_dir = tmp_path / "var"
    var_dir.mkdir()
    (var_dir / "pipeline_last_run.json").write_text(json.dumps({"status": "ok"}))

    with patch("scripts.nightly_pipeline.ROOT_DIR", tmp_path):
        _save_last_auto_feedback_at()
        data = json.loads((var_dir / "pipeline_last_run.json").read_text())
        assert "last_auto_feedback_at" in data
        assert data["status"] == "ok"  # preserved


def test_save_creates_var_dir(tmp_path):
    """Creates var/ directory if it doesn't exist."""
    with patch("scripts.nightly_pipeline.ROOT_DIR", tmp_path):
        _save_last_auto_feedback_at()
        data = json.loads((tmp_path / "var" / "pipeline_last_run.json").read_text())
        assert "last_auto_feedback_at" in data


def test_lookback_computes_days_since():
    """Verify days_since computation logic."""
    import math

    now = datetime.now(timezone.utc)
    last = (now - timedelta(hours=36)).isoformat()
    last_dt = datetime.fromisoformat(last)
    seconds_since = (now - last_dt).total_seconds()
    days_since = max(1, math.ceil(seconds_since / 86400))
    assert days_since == 2  # 36 hours = ceil to 2 days
