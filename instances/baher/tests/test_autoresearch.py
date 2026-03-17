"""Tests for BaherOS Autoresearch — mutator, scorer, optimizer."""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from app.autoresearch.mutator import (
    ConfigSurface,
    apply_mutation,
    describe_mutation,
    get_mutable_surfaces,
    revert_mutation,
)
from app.autoresearch.optimizer import (
    AutoresearchReport,
    format_report,
    run_autoresearch,
)
from app.autoresearch.run_log import ensure_table, log_iteration
from app.autoresearch.scorer import (
    Scorecard,
    compare_scorecards,
    scorecard_from_eval_result,
)
from app.evaluation.service import CaseResult, EvalSuiteResult

ROOT_DIR = Path(__file__).resolve().parents[1]


def _make_db(db_path: Path) -> None:
    schema_sql = (ROOT_DIR / "docs" / "schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        conn.commit()
    finally:
        conn.close()


def _make_configs(tmp_path: Path) -> Path:
    """Copy real configs to a temp dir so tests don't modify the originals."""
    configs_dir = tmp_path / "configs"
    shutil.copytree(ROOT_DIR / "configs", configs_dir)
    return configs_dir


def _seed_cases(db_path: Path, count: int = 3) -> None:
    conn = sqlite3.connect(db_path)
    for i in range(count):
        props = json.dumps({
            "mode": "work",
            "should_contain_keywords": ["pricing", "details"],
            "max_words": 150,
        })
        conn.execute(
            "INSERT INTO benchmark_cases (case_key, category, prompt_text, expected_properties_json) "
            "VALUES (?, ?, ?, ?)",
            (f"case_{i}", "work", f"Test prompt {i}", props),
        )
    conn.commit()
    conn.close()


# ── Mutator tests ──────────────────────────────────────────────────


def test_get_mutable_surfaces(tmp_path: Path) -> None:
    configs_dir = _make_configs(tmp_path)
    surfaces = get_mutable_surfaces(configs_dir)
    names = [s.name for s in surfaces]
    assert "top_k_reply_pairs" in names
    assert "top_k_chunks" in names
    assert "recency_boost_days" in names
    assert "recency_boost_weight" in names
    assert "account_boost_weight" in names
    assert "drafting_prompt" in names
    assert len(surfaces) == 6


def test_get_mutable_surfaces_filter_retrieval(tmp_path: Path) -> None:
    configs_dir = _make_configs(tmp_path)
    surfaces = get_mutable_surfaces(configs_dir, surface_filter="retrieval")
    names = [s.name for s in surfaces]
    assert "drafting_prompt" not in names
    assert len(surfaces) == 5


def test_get_mutable_surfaces_filter_prompt(tmp_path: Path) -> None:
    configs_dir = _make_configs(tmp_path)
    surfaces = get_mutable_surfaces(configs_dir, surface_filter="prompt_drafting")
    names = [s.name for s in surfaces]
    assert names == ["drafting_prompt"]


def test_numeric_mutation_and_revert(tmp_path: Path) -> None:
    configs_dir = _make_configs(tmp_path)
    surfaces = get_mutable_surfaces(configs_dir, surface_filter="retrieval")
    surface = next(s for s in surfaces if s.name == "top_k_reply_pairs")
    original = surface.current_value

    # Read original file bytes
    file_path = configs_dir / surface.config_file
    original_bytes = file_path.read_bytes()

    # Mutate
    old_val = apply_mutation(surface, configs_dir)
    assert old_val == original
    assert surface.current_value != original

    # Verify file changed
    data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    assert data["top_k_reply_pairs"] == surface.current_value

    # Revert
    revert_mutation(surface, old_val, configs_dir)
    assert surface.current_value == original

    # Verify reverted data matches
    data_after = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    assert data_after["top_k_reply_pairs"] == original


def test_numeric_mutation_respects_bounds(tmp_path: Path) -> None:
    configs_dir = _make_configs(tmp_path)
    surface = ConfigSurface(
        name="test_bounded",
        config_file="retrieval/defaults.yaml",
        yaml_key="top_k_reply_pairs",
        current_value=10,  # at max
        mutation_type="numeric_step",
        step_size=1,
        min_val=3,
        max_val=10,
    )
    # Should try decrement since at max
    old_val = apply_mutation(surface, configs_dir)
    assert surface.current_value == 9  # decremented


def test_template_variant_mutation_and_revert(tmp_path: Path) -> None:
    configs_dir = _make_configs(tmp_path)
    surfaces = get_mutable_surfaces(configs_dir, surface_filter="prompt_drafting")
    surface = surfaces[0]
    assert surface.name == "drafting_prompt"

    original = surface.current_value
    file_path = configs_dir / surface.config_file

    old_val = apply_mutation(surface, configs_dir)
    assert surface.current_value != original

    # Revert
    revert_mutation(surface, old_val, configs_dir)
    assert surface.current_value == original


def test_describe_mutation_numeric(tmp_path: Path) -> None:
    configs_dir = _make_configs(tmp_path)
    surfaces = get_mutable_surfaces(configs_dir, surface_filter="retrieval")
    surface = next(s for s in surfaces if s.name == "top_k_reply_pairs")
    desc = describe_mutation(surface)
    assert "top_k_reply_pairs" in desc
    assert "→" in desc


def test_describe_mutation_template(tmp_path: Path) -> None:
    configs_dir = _make_configs(tmp_path)
    surfaces = get_mutable_surfaces(configs_dir, surface_filter="prompt_drafting")
    desc = describe_mutation(surfaces[0])
    assert "drafting_prompt" in desc
    assert "variant" in desc


# ── Scorer tests ───────────────────────────────────────────────────


def test_scorecard_from_eval_result() -> None:
    cr = CaseResult(
        case_key="t", category="work", prompt_text="hi", draft="hello",
        detected_mode="work", confidence="high", precedent_count=2,
        scores={"keyword_hit_rate": 0.8, "confidence_score": 1.0},
        pass_fail="pass", notes="",
    )
    result = EvalSuiteResult(
        config_tag="v1", total_cases=1, passed=1, warned=0, failed=0,
        case_results=[cr], run_at="2026-03-15T00:00:00Z",
    )
    sc = scorecard_from_eval_result(result)
    assert sc.pass_rate == 1.0
    assert sc.avg_keyword_hit == 0.8
    assert sc.avg_confidence == 1.0
    # composite = 0.5*1.0 + 0.3*0.8 + 0.2*1.0 = 0.5 + 0.24 + 0.2 = 0.94
    assert abs(sc.composite - 0.94) < 0.01


def test_scorecard_from_empty_result() -> None:
    result = EvalSuiteResult(
        config_tag="empty", total_cases=0, passed=0, warned=0, failed=0,
        case_results=[], run_at="2026-03-15T00:00:00Z",
    )
    sc = scorecard_from_eval_result(result)
    assert sc.composite == 0.0


def test_scorecard_composite_formula() -> None:
    """Verify composite = 0.5*pass_rate + 0.3*avg_keyword_hit + 0.2*avg_confidence."""
    sc = Scorecard(
        config_tag="test",
        pass_rate=0.6, warn_rate=0.2, fail_rate=0.2,
        avg_keyword_hit=0.7, avg_confidence=0.5,
        composite=0.5 * 0.6 + 0.3 * 0.7 + 0.2 * 0.5,
    )
    expected = 0.5 * 0.6 + 0.3 * 0.7 + 0.2 * 0.5
    assert abs(sc.composite - expected) < 0.001


def test_compare_scorecards_improved() -> None:
    base = Scorecard("base", 0.5, 0.3, 0.2, 0.7, 0.5, composite=0.60)
    cand = Scorecard("cand", 0.6, 0.2, 0.2, 0.8, 0.6, composite=0.63)
    assert compare_scorecards(base, cand) == "improved"


def test_compare_scorecards_neutral() -> None:
    base = Scorecard("base", 0.5, 0.3, 0.2, 0.7, 0.5, composite=0.60)
    cand = Scorecard("cand", 0.5, 0.3, 0.2, 0.7, 0.5, composite=0.61)
    assert compare_scorecards(base, cand) == "neutral"


def test_compare_scorecards_regressed() -> None:
    base = Scorecard("base", 0.5, 0.3, 0.2, 0.7, 0.5, composite=0.60)
    cand = Scorecard("cand", 0.4, 0.3, 0.3, 0.6, 0.4, composite=0.55)
    assert compare_scorecards(base, cand) == "regressed"


def test_compare_scorecards_edge_exactly_improved() -> None:
    base = Scorecard("base", 0.5, 0.3, 0.2, 0.7, 0.5, composite=0.60)
    cand = Scorecard("cand", 0.5, 0.3, 0.2, 0.7, 0.5, composite=0.62)
    assert compare_scorecards(base, cand) == "improved"


def test_compare_scorecards_edge_barely_regressed() -> None:
    base = Scorecard("base", 0.5, 0.3, 0.2, 0.7, 0.5, composite=0.60)
    cand = Scorecard("cand", 0.5, 0.3, 0.2, 0.7, 0.5, composite=0.589)
    assert compare_scorecards(base, cand) == "regressed"


# ── Run log tests ──────────────────────────────────────────────────


def test_ensure_table_creates_table(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _make_db(db_path)
    database_url = f"sqlite:///{db_path}"
    ensure_table(database_url)

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='autoresearch_runs'"
    ).fetchone()
    conn.close()
    assert row is not None


def test_log_iteration_persists(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _make_db(db_path)
    database_url = f"sqlite:///{db_path}"
    ensure_table(database_url)

    log_iteration(
        database_url,
        run_tag="test_run",
        iteration=1,
        surface_name="top_k_reply_pairs",
        mutation_desc="top_k_reply_pairs: 5 → 6",
        baseline_composite=0.60,
        candidate_composite=0.63,
        outcome="improved",
        kept=True,
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM autoresearch_runs").fetchone()
    conn.close()

    assert row["run_tag"] == "test_run"
    assert row["surface_name"] == "top_k_reply_pairs"
    assert row["outcome"] == "improved"
    assert row["kept"] == 1


# ── Optimizer tests ────────────────────────────────────────────────


def _mock_generate_good(prompt_text: str, *, database_url: str, configs_dir: Path) -> dict[str, Any]:
    return {
        "draft": "Here is the pricing with details. Happy to help.",
        "detected_mode": "work",
        "confidence": "high",
        "precedent_count": 3,
    }


def _mock_generate_bad(prompt_text: str, *, database_url: str, configs_dir: Path) -> dict[str, Any]:
    return {
        "draft": "Hey buddy, what's up!",
        "detected_mode": "personal",
        "confidence": "low",
        "precedent_count": 0,
    }


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    configs_dir = _make_configs(tmp_path)
    db_path = tmp_path / "test.db"
    _make_db(db_path)
    database_url = f"sqlite:///{db_path}"

    # Snapshot config before
    retrieval_before = (configs_dir / "retrieval" / "defaults.yaml").read_bytes()
    prompts_before = (configs_dir / "prompts.yaml").read_bytes()

    report = run_autoresearch(
        configs_dir=configs_dir,
        database_url=database_url,
        generate_fn=_mock_generate_good,
        max_iterations=5,
        dry_run=True,
    )

    # Configs must be unchanged
    assert (configs_dir / "retrieval" / "defaults.yaml").read_bytes() == retrieval_before
    assert (configs_dir / "prompts.yaml").read_bytes() == prompts_before

    # Report should have surfaces listed
    assert len(report.iterations) > 0
    assert all(it.outcome == "dry_run" for it in report.iterations)
    assert report.total_eval_runs == 0


def test_optimizer_runs_and_respects_max_iterations(tmp_path: Path) -> None:
    configs_dir = _make_configs(tmp_path)
    db_path = tmp_path / "test.db"
    _make_db(db_path)
    _seed_cases(db_path, count=2)
    database_url = f"sqlite:///{db_path}"

    report = run_autoresearch(
        configs_dir=configs_dir,
        database_url=database_url,
        generate_fn=_mock_generate_good,
        max_iterations=3,
    )

    # Baseline counts as 1 eval, so at most 2 mutations
    assert report.total_eval_runs <= 3
    assert report.total_eval_runs >= 1


def test_optimizer_logs_to_db(tmp_path: Path) -> None:
    configs_dir = _make_configs(tmp_path)
    db_path = tmp_path / "test.db"
    _make_db(db_path)
    _seed_cases(db_path, count=2)
    database_url = f"sqlite:///{db_path}"

    report = run_autoresearch(
        configs_dir=configs_dir,
        database_url=database_url,
        generate_fn=_mock_generate_good,
        max_iterations=3,
    )

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM autoresearch_runs").fetchone()[0]
    conn.close()
    assert count == len(report.iterations)


def test_optimizer_reverts_on_regression(tmp_path: Path) -> None:
    """With a bad generate fn, mutations should regress and be reverted."""
    configs_dir = _make_configs(tmp_path)
    db_path = tmp_path / "test.db"
    _make_db(db_path)
    _seed_cases(db_path, count=2)
    database_url = f"sqlite:///{db_path}"

    # Snapshot before
    retrieval_before = yaml.safe_load(
        (configs_dir / "retrieval" / "defaults.yaml").read_text(encoding="utf-8")
    )

    report = run_autoresearch(
        configs_dir=configs_dir,
        database_url=database_url,
        generate_fn=_mock_generate_good,
        max_iterations=3,
        surface_filter="retrieval",
    )

    # All non-improved mutations should have been reverted
    for it in report.iterations:
        if not it.kept:
            assert it.outcome in ("neutral", "regressed")


def test_format_report_output() -> None:
    sc = Scorecard("test", 0.5, 0.3, 0.2, 0.7, 0.5, composite=0.60)
    report = AutoresearchReport(
        run_tag="test_run",
        started_at="2026-03-15T13:00:00Z",
        baseline=sc,
        final=sc,
        total_eval_runs=1,
    )
    output = format_report(report)
    assert "BaherOS Autoresearch" in output
    assert "Baseline" in output
    assert "Final" in output


# ── Git commit/tag tests for run_autoresearch.py ──────────────────

from unittest.mock import patch, MagicMock, call
from scripts.run_autoresearch import (
    _git_available,
    _git_commit_hash,
    _git_commit_kept_change,
    _git_tag_run,
    _log_git_hash_to_autoresearch_log,
)


def test_git_commit_called_after_kept_improvement() -> None:
    """After a kept improvement, git add + git commit should be called."""
    with patch("scripts.run_autoresearch.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        _git_commit_kept_change("top_k_reply_pairs", "8", "9", 0.6100, 0.6977)

        # Should have called git add and git commit
        assert mock_run.call_count == 2
        add_call = mock_run.call_args_list[0]
        assert "add" in add_call[0][0]
        assert "configs/retrieval/defaults.yaml" in add_call[0][0]
        assert "configs/prompts.yaml" in add_call[0][0]

        commit_call = mock_run.call_args_list[1]
        assert "commit" in commit_call[0][0]
        msg = commit_call[0][0][commit_call[0][0].index("-m") + 1]
        assert "autoresearch: keep top_k_reply_pairs" in msg
        assert "0.6100" in msg
        assert "0.6977" in msg


def test_git_tag_created_at_end_of_run_with_improvements() -> None:
    """A git tag should be created at end of run if improvements were kept."""
    with patch("scripts.run_autoresearch.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        _git_tag_run(0.6100, 0.7190, 2)

        assert mock_run.call_count == 1
        tag_call = mock_run.call_args[0][0]
        assert "tag" in tag_call
        assert any("autoresearch-" in arg for arg in tag_call)
        # Find the -m message
        m_idx = tag_call.index("-m")
        tag_msg = tag_call[m_idx + 1]
        assert "0.6100" in tag_msg
        assert "0.7190" in tag_msg
        assert "2 improvements" in tag_msg


def test_git_unavailable_handled_gracefully() -> None:
    """If git is not available, functions should not crash."""
    with patch("scripts.run_autoresearch.subprocess.run", side_effect=FileNotFoundError("git not found")):
        assert _git_available() is False
        assert _git_commit_hash() is None

    # _git_commit_kept_change and _git_tag_run should also not crash
    with patch("scripts.run_autoresearch.subprocess.run", side_effect=FileNotFoundError("git not found")):
        _git_commit_kept_change("test", "1", "2", 0.5, 0.6)  # should not raise

    with patch("scripts.run_autoresearch.subprocess.run", side_effect=FileNotFoundError("git not found")):
        _git_tag_run(0.5, 0.6, 1)  # should not raise


def test_log_git_hash_to_autoresearch_log(tmp_path: Path) -> None:
    """Starting an autoresearch run should log the git commit hash."""
    log_path = tmp_path / "autoresearch_log.md"
    log_path.write_text("# Log\n", encoding="utf-8")

    with patch("scripts.run_autoresearch.ROOT_DIR", tmp_path):
        with patch("scripts.run_autoresearch._git_commit_hash", return_value="abc123def456"):
            _log_git_hash_to_autoresearch_log()

    content = log_path.read_text(encoding="utf-8")
    assert "abc123def456" in content
    assert "Run started" in content
