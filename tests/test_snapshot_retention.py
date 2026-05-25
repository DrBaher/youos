"""Snapshot retention policy automation.

The primitive (``app.core.data_safety.prune_snapshots``) already existed,
but nothing called it on a schedule. To prune you had to either run
``youos snapshot-create`` (which pruned as a side-effect) or write your
own cron. This module pins:

1. **`prune_snapshots` returns a per-tier count of deletions** (used to be
   `None`) so the nightly and the CLI can report what they did.
2. **Retention limits come from `snapshots.keep_{hourly,daily,manual}`**
   in the instance config, with fallback to the historical defaults
   (72 / 30 / 50). Explicit kwargs still win — back-compat with the
   existing test/call sites that pass them.
3. **`step_snapshot_daily` runs as the first step of the nightly**, takes
   a snapshot of pre-pipeline state, then prunes. Skips silently on a
   fresh instance (DB doesn't exist yet) so a pre-first-ingest nightly
   doesn't litter the snapshots dir.
4. **`youos snapshot-prune` exposes the same primitive at the CLI** so
   the user can prune manually without taking a snapshot first.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest


@pytest.fixture
def _reset_settings():
    from app.core.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def instance_with_db(monkeypatch, tmp_path, _reset_settings):
    """A YOUOS_DATA_DIR with a minimal SQLite DB."""
    (tmp_path / "var").mkdir()
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    db = tmp_path / "var" / "youos.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.commit()
    conn.close()
    return tmp_path


def _seed_snapshots(db_path: Path, tier: str, n: int) -> list[Path]:
    """Drop *n* fake snapshot files into the tier dir with distinct mtimes."""
    tier_dir = db_path.parent / "snapshots" / tier
    tier_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for i in range(n):
        f = tier_dir / f"youos-2026010{i % 10}-{i:06d}.db"
        f.write_bytes(b"fake")
        # Stagger mtimes so the prune's mtime-desc sort is deterministic.
        t = time.time() - (n - i) * 60
        import os

        os.utime(f, (t, t))
        files.append(f)
    return files


# ── prune_snapshots: signature change is back-compat ─────────────────────

def test_prune_with_no_kwargs_uses_historical_defaults(instance_with_db):
    """Existing callers (e.g. `youos snapshot-create`) that pass no kwargs
    must keep the historical limits exactly: 72 hourly, 30 daily, 50 manual."""
    from app.core.data_safety import prune_snapshots

    db = instance_with_db / "var" / "youos.db"
    _seed_snapshots(db, "hourly", 75)  # > 72 → 3 should be pruned
    _seed_snapshots(db, "daily", 35)   # > 30 → 5 should be pruned
    _seed_snapshots(db, "manual", 55)  # > 50 → 5 should be pruned

    removed = prune_snapshots(db)
    assert removed == {"hourly": 3, "daily": 5, "manual": 5}


def test_prune_returns_per_tier_counts(instance_with_db):
    """Used to return None; now returns a dict so callers can report
    what was actually pruned without re-counting."""
    from app.core.data_safety import prune_snapshots

    db = instance_with_db / "var" / "youos.db"
    _seed_snapshots(db, "hourly", 10)

    removed = prune_snapshots(db, keep_hourly=5, keep_daily=100, keep_manual=100)
    assert removed["hourly"] == 5
    assert removed["daily"] == 0
    assert removed["manual"] == 0


def test_prune_skips_missing_tier_dirs(instance_with_db):
    """A tier dir that doesn't exist isn't an error — just nothing to prune."""
    from app.core.data_safety import prune_snapshots

    db = instance_with_db / "var" / "youos.db"
    # No snapshots created at all.
    removed = prune_snapshots(db)
    assert removed == {"hourly": 0, "daily": 0, "manual": 0}


# ── retention comes from config when kwargs absent ───────────────────────

def test_prune_reads_retention_limits_from_config(monkeypatch, instance_with_db):
    from app.core.data_safety import prune_snapshots

    # Config says keep only 2 hourly; defaults would keep 72.
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **kw: {"snapshots": {"keep_hourly": 2, "keep_daily": 1}},
    )

    db = instance_with_db / "var" / "youos.db"
    _seed_snapshots(db, "hourly", 10)
    _seed_snapshots(db, "daily", 5)

    removed = prune_snapshots(db)
    assert removed["hourly"] == 8  # 10 - 2
    assert removed["daily"] == 4   # 5 - 1
    # `keep_manual` absent from config → falls back to 50.
    assert removed["manual"] == 0


def test_explicit_kwargs_override_config(monkeypatch, instance_with_db):
    """A per-call kwarg wins over the YAML knob — useful for one-off
    aggressive prunes from the CLI."""
    from app.core.data_safety import prune_snapshots

    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **kw: {"snapshots": {"keep_hourly": 100}},
    )

    db = instance_with_db / "var" / "youos.db"
    _seed_snapshots(db, "hourly", 10)

    removed = prune_snapshots(db, keep_hourly=2)
    assert removed["hourly"] == 8


def test_prune_ignores_invalid_config_types(monkeypatch, instance_with_db):
    """A fat-fingered ``keep_hourly: "lots"`` shouldn't crash — fall back
    to the historical default rather than passing a string to slicing."""
    from app.core.data_safety import prune_snapshots

    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **kw: {"snapshots": {"keep_hourly": "lots", "keep_daily": -5}},
    )

    db = instance_with_db / "var" / "youos.db"
    # 75 hourly. With historical default of 72 → 3 pruned.
    _seed_snapshots(db, "hourly", 75)
    removed = prune_snapshots(db)
    assert removed["hourly"] == 3


def test_prune_handles_missing_config_module(monkeypatch, instance_with_db):
    """If `load_config` blows up (e.g. malformed YAML), the prune should
    still proceed with defaults rather than failing the nightly."""
    from app.core.data_safety import prune_snapshots

    def _boom(*a, **kw):
        raise RuntimeError("malformed YAML")

    monkeypatch.setattr("app.core.config.load_config", _boom)

    db = instance_with_db / "var" / "youos.db"
    _seed_snapshots(db, "hourly", 75)
    removed = prune_snapshots(db)
    assert removed["hourly"] == 3  # used historical default of 72


# ── nightly step ─────────────────────────────────────────────────────────

def test_step_snapshot_daily_creates_snapshot_and_prunes(instance_with_db, monkeypatch):
    """End-to-end: nightly step takes a daily snapshot, file exists, prune
    runs (returns 0 since there's only one snapshot in this tier)."""
    monkeypatch.setattr("sys.argv", ["nightly_pipeline.py"])
    from scripts.nightly_pipeline import step_snapshot_daily

    result = step_snapshot_daily(verbose=False)
    assert result["ok"] is True
    assert result["skipped"] is False
    snap = Path(result["snapshot_path"])
    assert snap.exists()
    assert snap.parent.name == "daily"
    # Pruned = 0 because there's only one daily snapshot.
    assert result["pruned"] == {"hourly": 0, "daily": 0, "manual": 0}


def test_step_snapshot_daily_skips_on_fresh_instance(monkeypatch, tmp_path, _reset_settings):
    """A pre-first-ingest instance has no DB — snapshot would create an
    empty file. Skip silently so the snapshots dir isn't polluted."""
    (tmp_path / "var").mkdir()
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))

    # Force-reimport so DEFAULT_DB picks up the new env.
    import importlib
    import sys

    sys.modules.pop("scripts.nightly_pipeline", None)
    np_mod = importlib.import_module("scripts.nightly_pipeline")

    result = np_mod.step_snapshot_daily(verbose=False)
    assert result == {"ok": True, "skipped": True}
    # And — most importantly — no DB file was created.
    assert not (tmp_path / "var" / "youos.db").exists()


def test_step_snapshot_daily_is_first_step_in_nightly_log(monkeypatch, instance_with_db):
    """Snapshot runs *before* dedup/ingestion so the snapshot reflects
    pre-pipeline state — if a downstream step corrupts the DB the user can
    restore from this morning's snapshot."""
    import json

    import scripts.nightly_pipeline as np_mod

    def _no(*_a, **_kw): return True

    def _no_dict(*_a, **_kw): return {"captured": 0, "total": 0, "skipped": 0, "errors": 0}

    def _no_embed(*_a, **_kw): return {"ok": True}

    monkeypatch.setattr(np_mod, "step_deduplicate", _no)
    monkeypatch.setattr(np_mod, "step_ingest_gmail", _no)
    monkeypatch.setattr(np_mod, "step_auto_feedback", _no_dict)
    monkeypatch.setattr(np_mod, "step_export_feedback", _no)
    monkeypatch.setattr(np_mod, "step_finetune_lora", _no)
    monkeypatch.setattr(np_mod, "step_golden_eval", _no)
    monkeypatch.setattr(np_mod, "step_index_embeddings", _no_embed)
    monkeypatch.setattr(np_mod, "step_autoresearch", _no)
    monkeypatch.setattr(np_mod, "should_skip_dedup", lambda _db: (False, ""))
    monkeypatch.setattr(np_mod, "should_skip_finetune", lambda _db: (False, ""))
    monkeypatch.setattr(np_mod, "should_skip_embeddings", lambda _db: (False, ""))
    monkeypatch.setattr(np_mod, "should_skip_autoresearch", lambda _db: (False, ""))
    monkeypatch.setattr(np_mod, "_count_unused_feedback", lambda _db: 0)
    monkeypatch.setattr("sys.argv", ["nightly_pipeline.py"])

    np_mod.main()

    log = json.loads((instance_with_db / "var" / "pipeline_last_run.json").read_text())
    # daily_snapshot exists as a tracked step
    assert "daily_snapshot" in log["steps"]
    assert log["steps"]["daily_snapshot"] is True
    # And got a duration recorded — confirms it ran through the timing wrapper.
    assert "daily_snapshot" in log["step_durations"]


# ── CLI command ──────────────────────────────────────────────────────────

def test_cli_snapshot_prune_reports_per_tier_counts(instance_with_db, capsys):
    """`youos snapshot-prune` calls the same primitive and prints what was
    deleted so the user can confirm the policy did what they expected."""
    from typer.testing import CliRunner

    from app.cli import app

    db = instance_with_db / "var" / "youos.db"
    _seed_snapshots(db, "hourly", 10)

    runner = CliRunner()
    result = runner.invoke(app, ["snapshot-prune", "--keep-hourly", "3"])
    assert result.exit_code == 0
    # 7 hourly snapshots removed (10 - 3); daily / manual untouched.
    assert "hourly: pruned 7" in result.output
    assert "daily: pruned 0" in result.output
    assert "manual: pruned 0" in result.output
    assert "total pruned: 7" in result.output
