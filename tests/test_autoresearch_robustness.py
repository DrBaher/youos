"""Tests for the operational fixes that let autoresearch actually run:

- benchmark_cases auto-seeding from golden.yaml (so a fresh instance works)
- a subprocess helper with a hard, process-group timeout (so a stalled `claude`
  generation can't hang the whole loop)
- eval-suite tolerance of per-case generation failures
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time

import pytest

from app.evaluation.service import (
    EvalRequest,
    load_benchmark_cases,
    run_eval_suite,
    seed_benchmark_cases_from_golden,
)
from app.generation.service import _run_subprocess

# ── #3 benchmark seeding ──────────────────────────────────────────────


def test_seed_from_golden_populates_empty_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    try:
        n = seed_benchmark_cases_from_golden(conn)
        assert n >= 1
        assert conn.execute("SELECT COUNT(*) FROM benchmark_cases").fetchone()[0] == n
    finally:
        conn.close()


def test_seed_is_idempotent(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    try:
        first = seed_benchmark_cases_from_golden(conn)
        second = seed_benchmark_cases_from_golden(conn)
        assert first >= 1
        assert second == 0  # INSERT OR IGNORE — no duplicates on re-run
    finally:
        conn.close()


def test_load_benchmark_cases_auto_seeds_missing_table(tmp_path):
    conn = sqlite3.connect(tmp_path / "fresh.db")  # table does not exist yet
    try:
        cases = load_benchmark_cases(conn)
        assert len(cases) >= 1
        ep = json.loads(cases[0]["expected_properties_json"])
        # Keys evaluate_case actually reads.
        assert "should_contain_keywords" in ep
        assert "mode" in ep
    finally:
        conn.close()


# ── #2 subprocess hard timeout ────────────────────────────────────────


def test_run_subprocess_returns_output():
    r = _run_subprocess(["echo", "hello"], timeout=5)
    assert r.returncode == 0
    assert "hello" in r.stdout


def test_run_subprocess_times_out_promptly():
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _run_subprocess(["sleep", "10"], timeout=1)
    # The process-group kill must release us near the timeout, not at sleep's end.
    assert time.monotonic() - start < 5


# ── #2 eval tolerates a failing/timed-out generation ──────────────────


def test_eval_suite_tolerates_generation_failure(tmp_path):
    db = tmp_path / "e.db"
    conn = sqlite3.connect(db)
    seed_benchmark_cases_from_golden(conn)
    conn.close()

    def boom(prompt, *, database_url, configs_dir):
        raise RuntimeError("simulated generation timeout")

    result = run_eval_suite(
        EvalRequest(config_tag="robustness-test"),
        generate_fn=boom,
        database_url=f"sqlite:///{db}",
        configs_dir=tmp_path,
        persist=False,
    )
    # Every case ran (and scored as a fail) instead of the suite aborting.
    assert result.total_cases >= 1
