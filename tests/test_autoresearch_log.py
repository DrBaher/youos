"""Tests for structured autoresearch JSON run log."""

import json

from app.autoresearch.optimizer import AutoresearchReport, IterationResult, _write_jsonl_entry
from app.autoresearch.scorer import Scorecard


def _make_scorecard(composite: float = 0.75) -> Scorecard:
    return Scorecard(
        config_tag="test",
        pass_rate=0.8,
        warn_rate=0.1,
        fail_rate=0.1,
        avg_keyword_hit=0.5,
        avg_confidence=0.7,
        composite=composite,
    )


def test_write_jsonl_entry(tmp_path):
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    var_dir = tmp_path / "var"

    report = AutoresearchReport(
        run_tag="test_run",
        started_at="2026-03-16T10:00:00Z",
        baseline=_make_scorecard(0.50),
        final=_make_scorecard(0.75),
        iterations=[
            IterationResult(
                iteration=1,
                surface_name="retrieval.top_k",
                mutation_desc="top_k 5 -> 7",
                baseline_composite=0.50,
                candidate_composite=0.75,
                outcome="improved",
                kept=True,
            ),
            IterationResult(
                iteration=2,
                surface_name="prompt.temperature",
                mutation_desc="temp 0.7 -> 0.8",
                baseline_composite=0.75,
                candidate_composite=0.60,
                outcome="regressed",
                kept=False,
            ),
        ],
        total_eval_runs=3,
        improvements_kept=1,
        reverted=1,
    )

    _write_jsonl_entry(report, configs_dir)

    jsonl_path = var_dir / "autoresearch_runs.jsonl"
    assert jsonl_path.exists()

    lines = jsonl_path.read_text().strip().splitlines()
    assert len(lines) == 1

    entry = json.loads(lines[0])
    assert entry["run_at"] == "2026-03-16T10:00:00Z"
    assert entry["iterations"] == 3
    assert entry["composite_score"] == 0.75
    assert entry["improvements"] == ["retrieval.top_k"]
    assert entry["regressions"] == ["prompt.temperature"]
    assert entry["config_snapshot"]["improvements_kept"] == 1


def test_jsonl_append(tmp_path):
    """Multiple writes should append, not overwrite."""
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()

    for i in range(3):
        report = AutoresearchReport(
            run_tag=f"run_{i}",
            started_at=f"2026-03-1{i}T10:00:00Z",
            baseline=_make_scorecard(0.5),
            final=_make_scorecard(0.5 + i * 0.1),
            total_eval_runs=1,
        )
        _write_jsonl_entry(report, configs_dir)

    jsonl_path = tmp_path / "var" / "autoresearch_runs.jsonl"
    lines = jsonl_path.read_text().strip().splitlines()
    assert len(lines) == 3
