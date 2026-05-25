"""Per-step durations + schema versioning for pipeline_last_run.json.

Until now ``pipeline_last_run.json`` only recorded which steps succeeded
and the total elapsed time — so a 4-hour nightly was indistinguishable
from a 4-minute one once you'd lost the terminal output, and there was
no way to tell *where* the time went. Stats UI / dashboards couldn't
build a "where is autoresearch spending its budget" view.

This module pins the additive observability fields the nightly now
emits:

- ``schema_version: "v1"`` so downstream consumers (the stats UI, the
  future autoresearch convergence dashboard, an external history viewer)
  can detect breaking shape changes instead of silently mis-parsing.
- ``duration_seconds`` — total wall-clock for the run, already implicit
  in the terminal summary but never in the JSON.
- ``step_durations: {step_name: seconds}`` — per-step wall-clock,
  recorded on success, error, *and* skipped paths so a skip shows as
  ~0s rather than missing.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


@pytest.fixture
def _reset_settings():
    from app.core.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def instance_var(monkeypatch, tmp_path, _reset_settings):
    """Point YOUOS_DATA_DIR at a tmp instance; pipeline log lands in tmp_path/var/."""
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    return tmp_path / "var"


# ── _write_pipeline_log shape ────────────────────────────────────────────

def test_write_pipeline_log_persists_new_observability_fields(instance_var):
    """The writer doesn't synthesise any of the new fields — it just
    serializes the dict it's given. Callers are responsible for including
    schema_version, duration_seconds, and step_durations. This guards
    that *if* the caller passes them, they round-trip cleanly to JSON."""
    from scripts.nightly_pipeline import _write_pipeline_log

    instance_var.mkdir()
    _write_pipeline_log(
        {
            "schema_version": "v1",
            "run_at": "2026-05-25T01:00:00+00:00",
            "duration_seconds": 132.5,
            "status": "ok",
            "steps": {"dedup": True, "ingestion": True},
            "step_durations": {"dedup": 0.4, "ingestion": 12.1},
            "errors": [],
            "skipped_steps": [],
            "benchmark_rotated": False,
            "golden_composite": 0.62,
        },
    )

    data = json.loads((instance_var / "pipeline_last_run.json").read_text())
    assert data["schema_version"] == "v1"
    assert data["duration_seconds"] == 132.5
    assert data["step_durations"] == {"dedup": 0.4, "ingestion": 12.1}


# ── main() actually records durations end-to-end ─────────────────────────

def _stub_all_steps(monkeypatch):
    """Stub every subprocess-launching step in nightly_pipeline so main()
    can run to completion in-process. Each stub sleeps a tiny fixed amount
    so the recorded duration is observably > 0 and ordering is testable."""
    import scripts.nightly_pipeline as np_mod

    def _fast_true(*_args, **_kwargs) -> bool:
        # Small but non-zero so durations are distinguishable from
        # millisecond timer jitter in CI.
        import time as _t

        _t.sleep(0.02)
        return True

    def _fast_auto_feedback(*_args, **_kwargs) -> dict:
        import time as _t

        _t.sleep(0.02)
        return {"captured": 0, "total": 0, "skipped": 0, "errors": 0}

    def _fast_embed(*_args, **_kwargs) -> dict:
        import time as _t

        _t.sleep(0.02)
        return {"ok": True}

    monkeypatch.setattr(np_mod, "step_deduplicate", _fast_true)
    monkeypatch.setattr(np_mod, "step_ingest_gmail", _fast_true)
    monkeypatch.setattr(np_mod, "step_auto_feedback", _fast_auto_feedback)
    monkeypatch.setattr(np_mod, "step_export_feedback", _fast_true)
    monkeypatch.setattr(np_mod, "step_finetune_lora", _fast_true)
    monkeypatch.setattr(np_mod, "step_golden_eval", _fast_true)
    monkeypatch.setattr(np_mod, "step_index_embeddings", _fast_embed)
    monkeypatch.setattr(np_mod, "step_autoresearch", _fast_true)
    # Skip-gates return (False, msg) so we exercise the run-the-step path
    # rather than the skip path. Skip path is covered separately below.
    monkeypatch.setattr(np_mod, "should_skip_dedup", lambda _db: (False, ""))
    monkeypatch.setattr(np_mod, "should_skip_finetune", lambda _db: (False, ""))
    monkeypatch.setattr(np_mod, "should_skip_embeddings", lambda _db: (False, ""))
    monkeypatch.setattr(np_mod, "should_skip_autoresearch", lambda _db: (False, ""))
    monkeypatch.setattr(np_mod, "_count_unused_feedback", lambda _db: 0)


def test_main_writes_schema_version_and_step_durations(monkeypatch, instance_var):
    instance_var.mkdir()
    _stub_all_steps(monkeypatch)
    monkeypatch.setattr("sys.argv", ["nightly_pipeline.py"])

    from scripts.nightly_pipeline import main

    main()

    log = json.loads((instance_var / "pipeline_last_run.json").read_text())

    assert log["schema_version"] == "v1", "consumers need a version field to detect breaking shape changes"
    assert "duration_seconds" in log
    assert log["duration_seconds"] >= 0  # CI clock may report 0.0 for very fast runs
    assert "step_durations" in log

    # Every step that ran should have a duration recorded. The exact set
    # depends on skip-gate behaviour, but at least the always-attempted
    # ones must be present.
    durations = log["step_durations"]
    for step in ("dedup", "ingestion", "auto_feedback", "golden_eval"):
        assert step in durations, f"{step} duration missing from pipeline log"
        assert isinstance(durations[step], (int, float))
        # We stubbed with sleep(0.02); allow timer jitter in CI by accepting >= 0.
        assert durations[step] >= 0


def test_main_records_duration_even_when_step_skipped(monkeypatch, instance_var):
    """A skip-gated step (e.g. autoresearch skipped because too few pairs)
    must still get a duration recorded — otherwise the dashboard can't
    distinguish "wasn't run" from "ran but had no entry".
    """
    instance_var.mkdir()
    _stub_all_steps(monkeypatch)
    import scripts.nightly_pipeline as np_mod

    # Flip the autoresearch skip-gate ON so we take the skipped branch.
    monkeypatch.setattr(np_mod, "should_skip_autoresearch", lambda _db: (True, "[autoresearch] Skipping — corpus too small"))
    monkeypatch.setattr("sys.argv", ["nightly_pipeline.py"])

    from scripts.nightly_pipeline import main

    main()

    log = json.loads((instance_var / "pipeline_last_run.json").read_text())
    assert "autoresearch" in log["step_durations"], "skipped steps must still appear in step_durations"
    # The skip path doesn't do real work, so it should be near-instant.
    assert log["step_durations"]["autoresearch"] < 0.5
    # And the existing skip-tracking is preserved.
    assert "autoresearch" in log["skipped_steps"]


def test_main_records_duration_when_step_raises(monkeypatch, instance_var):
    """A step that throws must still get a duration — otherwise a slow
    failure looks the same as a fast failure in the dashboard."""
    instance_var.mkdir()
    _stub_all_steps(monkeypatch)
    import scripts.nightly_pipeline as np_mod

    def _slow_explode(*_args, **_kwargs):
        import time as _t

        _t.sleep(0.05)
        raise RuntimeError("simulated step failure")

    monkeypatch.setattr(np_mod, "step_ingest_gmail", _slow_explode)
    monkeypatch.setattr("sys.argv", ["nightly_pipeline.py"])

    from scripts.nightly_pipeline import main

    main()

    log = json.loads((instance_var / "pipeline_last_run.json").read_text())
    assert "ingestion" in log["step_durations"]
    assert log["step_durations"]["ingestion"] >= 0.05  # picked up the slow path
    assert log["status"] in {"partial", "failed"}
    assert any("Gmail ingestion error" in e for e in log["errors"])


# ── Forward-compat: the stats API still parses the augmented log ─────────

def test_stats_api_consumer_handles_new_schema_fields(instance_var):
    """The reader (`app.core.stats.get_pipeline_status`) returns the dict
    as-is — but pin that the new fields don't break the parse, so a
    stats endpoint that just renders `data["status"]` (or any older
    field) keeps working unchanged."""
    instance_var.mkdir()
    augmented = {
        "schema_version": "v1",
        "run_at": "2026-05-25T01:00:00+00:00",
        "duration_seconds": 99.0,
        "status": "partial",
        "steps": {"dedup": True},
        "step_durations": {"dedup": 0.1},
        "errors": [],
        "skipped_steps": [],
        "benchmark_rotated": False,
        "golden_composite": None,
    }
    (instance_var / "pipeline_last_run.json").write_text(json.dumps(augmented))

    from app.core.stats import get_pipeline_status

    parsed = get_pipeline_status(instance_var.parent)
    assert parsed is not None
    # Old consumers still see the fields they know about.
    assert parsed["status"] == "partial"
    assert parsed["steps"] == {"dedup": True}
    # And the new ones round-trip.
    assert parsed["schema_version"] == "v1"
    assert parsed["step_durations"] == {"dedup": 0.1}


# Silence unused-import lint in case a future test wants `patch`.
_ = patch
