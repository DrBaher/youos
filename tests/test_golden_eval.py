"""Tests for golden benchmark evaluation (Item 11)."""

from __future__ import annotations

from pathlib import Path

import yaml

from scripts.run_golden_eval import (
    format_scorecard,
    load_golden_cases,
    run_golden_eval,
    save_results,
    score_case,
)


def test_load_golden_cases():
    """Golden cases load from configs/benchmarks/golden.yaml."""
    cases = load_golden_cases()
    assert len(cases) == 5
    ids = [c["id"] for c in cases]
    assert "golden-schedule-meeting" in ids
    assert "golden-decline-request" in ids
    assert "golden-follow-up-proposal" in ids
    assert "golden-thank-intro" in ids
    assert "golden-ask-clarification" in ids


def test_golden_case_structure():
    """Each golden case has required fields."""
    cases = load_golden_cases()
    for case in cases:
        assert "id" in case
        assert "description" in case
        assert "inbound" in case
        assert "expected_keywords" in case
        assert "expected_mode" in case
        assert "max_words" in case


def test_score_case_pass():
    """Score a case that passes all checks."""
    case = {
        "id": "test",
        "expected_keywords": ["hello", "world"],
        "expected_mode": "work",
        "max_words": 50,
    }
    result = score_case(case, "Hello world, nice to meet you.", "work")
    assert result["status"] == "pass"
    assert result["keyword_hit_rate"] == 1.0
    assert result["mode_match"] is True
    assert result["brevity_pass"] is True


def test_score_case_fail():
    """Score a case that fails."""
    case = {
        "id": "test",
        "expected_keywords": ["alpha", "beta", "gamma"],
        "expected_mode": "work",
        "max_words": 10,
    }
    result = score_case(case, "This is a very long response that has absolutely nothing to do with the expected keywords at all.", "personal")
    assert result["status"] == "fail"
    assert result["mode_match"] is False


def test_score_case_warn():
    """Score a case that gets warn status."""
    case = {
        "id": "test",
        "expected_keywords": ["hello", "world", "foo", "bar"],
        "expected_mode": "work",
        "max_words": 50,
    }
    # 1/4 keywords = 0.25, mode match, brevity pass -> warn
    result = score_case(case, "hello there", "work")
    assert result["status"] == "warn"


def test_run_golden_eval_without_generate():
    """Golden eval runs without a generate function (dry run)."""
    summary = run_golden_eval()
    assert summary["total"] == 5
    assert summary["failed"] == 5  # all fail with empty drafts


def test_run_golden_eval_with_mock_generate(tmp_path):
    """Golden eval with mock generate function."""
    golden_path = tmp_path / "golden.yaml"
    golden_path.write_text(yaml.dump({
        "cases": [{
            "id": "test-1",
            "description": "Test case",
            "inbound": "Can we meet?",
            "expected_keywords": ["available", "time"],
            "expected_mode": "work",
            "max_words": 50,
        }]
    }))

    def mock_generate(prompt, *, database_url, configs_dir):
        return {"draft": "I'm available. What time works?", "detected_mode": "work"}

    summary = run_golden_eval(
        generate_fn=mock_generate,
        database_url="sqlite:///test.db",
        configs_dir=tmp_path,
        golden_path=golden_path,
    )
    assert summary["total"] == 1
    assert summary["passed"] == 1


def test_save_and_format_results(tmp_path):
    """Results can be saved to JSON and formatted as scorecard."""
    summary = run_golden_eval()
    path = tmp_path / "results.json"
    save_results(summary, path)
    assert path.exists()

    scorecard = format_scorecard(summary)
    assert "Golden Benchmark Results" in scorecard
    assert "Total: 5" in scorecard
