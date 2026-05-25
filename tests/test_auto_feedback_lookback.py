"""Tests for auto-feedback lookback window (Item 5)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts.nightly_pipeline import _load_last_auto_feedback_at, _save_last_auto_feedback_at


@pytest.fixture
def _reset_settings():
    """Clear the lru_cache on get_settings so YOUOS_DATA_DIR monkeypatching takes effect."""
    from app.core.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def instance_var(monkeypatch, tmp_path, _reset_settings):
    """Point YOUOS_DATA_DIR at a tmp instance — pipeline log writes land in tmp_path/var/."""
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    return tmp_path / "var"


def test_load_last_auto_feedback_at_missing(instance_var):
    """Returns None when file doesn't exist."""
    assert _load_last_auto_feedback_at() is None


def test_load_last_auto_feedback_at_present(instance_var):
    """Returns timestamp when present in pipeline log."""
    instance_var.mkdir()
    ts = "2026-03-14T10:00:00+00:00"
    (instance_var / "pipeline_last_run.json").write_text(json.dumps({"last_auto_feedback_at": ts}))

    assert _load_last_auto_feedback_at() == ts


def test_save_last_auto_feedback_at(instance_var):
    """Saves timestamp to pipeline log, preserving existing data."""
    instance_var.mkdir()
    (instance_var / "pipeline_last_run.json").write_text(json.dumps({"status": "ok"}))

    _save_last_auto_feedback_at()
    data = json.loads((instance_var / "pipeline_last_run.json").read_text())
    assert "last_auto_feedback_at" in data
    assert data["status"] == "ok"  # preserved


def test_save_creates_var_dir(instance_var):
    """Creates var/ directory if it doesn't exist."""
    _save_last_auto_feedback_at()
    data = json.loads((instance_var / "pipeline_last_run.json").read_text())
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
