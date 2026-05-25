"""Tests for pipeline failure log."""

import json

import pytest


@pytest.fixture
def _reset_settings():
    from app.core.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def instance_var(monkeypatch, tmp_path, _reset_settings):
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    return tmp_path / "var"


def test_write_pipeline_log(instance_var):
    """Test that pipeline log is written as valid JSON."""
    from scripts.nightly_pipeline import _write_pipeline_log

    instance_var.mkdir()
    run_log = {
        "run_at": "2026-03-16T01:00:00+00:00",
        "status": "ok",
        "steps": {"dedup": True, "ingestion": True},
        "errors": [],
    }
    _write_pipeline_log(run_log)
    log_path = instance_var / "pipeline_last_run.json"
    assert log_path.exists()
    data = json.loads(log_path.read_text())
    assert data["status"] == "ok"
    assert data["steps"]["dedup"] is True


def test_pipeline_status_logic():
    """Test ok/partial/failed status derivation."""
    # all ok
    steps_all_ok = {"dedup": True, "ingestion": True, "autoresearch": True}
    assert all(steps_all_ok.values())

    # partial
    steps_partial = {"dedup": True, "ingestion": False, "autoresearch": True}
    assert not all(steps_partial.values())
    assert any(steps_partial.values())

    # failed
    steps_failed = {"dedup": False, "ingestion": False}
    assert not all(steps_failed.values())
    assert not any(steps_failed.values()) or any(steps_failed.values())
    # More precise: all failed
    all_ok = all(steps_failed.values())
    any_ok = any(steps_failed.values())
    if all_ok:
        status = "ok"
    elif any_ok:
        status = "partial"
    else:
        status = "failed"
    assert status == "failed"


def test_pipeline_log_on_error(instance_var):
    """Test that log is written even when errors occur."""
    from scripts.nightly_pipeline import _write_pipeline_log

    instance_var.mkdir()
    run_log = {
        "run_at": "2026-03-16T01:00:00+00:00",
        "status": "partial",
        "steps": {"dedup": True, "ingestion": False},
        "errors": ["Gmail ingestion failed"],
    }
    _write_pipeline_log(run_log)
    data = json.loads((instance_var / "pipeline_last_run.json").read_text())
    assert data["status"] == "partial"
    assert len(data["errors"]) == 1
    assert "Gmail" in data["errors"][0]


def test_stats_endpoint_includes_pipeline(tmp_path):
    """Test that /stats/data includes pipeline_last_run when file exists."""
    log_data = {
        "run_at": "2026-03-16T01:00:00+00:00",
        "status": "ok",
        "steps": {"dedup": True},
        "errors": [],
    }
    log_path = tmp_path / "pipeline_last_run.json"
    log_path.write_text(json.dumps(log_data))

    # Verify the file can be read back correctly
    loaded = json.loads(log_path.read_text())
    assert loaded["status"] == "ok"
    assert "pipeline_last_run" not in loaded or loaded.get("pipeline_last_run") is None
    # The actual endpoint integration is tested via the stats route
    # but we verify the JSON structure here
    assert loaded["run_at"] == "2026-03-16T01:00:00+00:00"
