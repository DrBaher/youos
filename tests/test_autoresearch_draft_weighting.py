"""Draft-quality-weighted autoresearch objective.

Feeds the draft_events signal into autoresearch: golden-eval cases are weighted
by how much real drafts for that case's sender_type cohort get edited, so the
optimizer prioritizes configs that help where drafting actually struggles.
Default-off; no data / disabled → equal weighting (unchanged).
"""

from __future__ import annotations

from app.autoresearch.scorer import (
    draft_quality_case_weights,
    draft_quality_weighting_enabled,
    scorecard_from_eval_result,
)
from app.evaluation.service import CaseResult, EvalSuiteResult


def _case(category: str, pass_fail: str, kw: float, conf: float) -> CaseResult:
    return CaseResult(
        case_key=f"{category}-{pass_fail}", category=category, prompt_text="p", draft="d",
        detected_mode="work", confidence="high", precedent_count=0,
        scores={"keyword_hit_rate": kw, "confidence_score": conf}, pass_fail=pass_fail, notes="",
    )


def _suite(cases: list[tuple[str, str, float, float]]) -> EvalSuiteResult:
    crs = [_case(*c) for c in cases]
    return EvalSuiteResult(
        config_tag="t", total_cases=len(crs),
        passed=sum(1 for c in crs if c.pass_fail == "pass"),
        warned=sum(1 for c in crs if c.pass_fail == "warn"),
        failed=sum(1 for c in crs if c.pass_fail == "fail"),
        case_results=crs, run_at="now",
    )


# --- weight derivation -----------------------------------------------------


def test_case_weights_scale_with_edit_distance():
    summary = {
        "outcome": {
            "avg_edit_distance_by_sender_type": {
                "internal": {"avg_edit_distance": 0.1, "n": 5},
                "external_client": {"avg_edit_distance": 0.5, "n": 5},
            }
        }
    }
    w = draft_quality_case_weights(summary)  # k=2.0
    assert w["internal"] == 1.2
    assert w["external_client"] == 2.0
    assert w["external_client"] > w["internal"]  # heavier-edited cohort weighted more


def test_case_weights_clamp_at_max():
    summary = {"outcome": {"avg_edit_distance_by_sender_type": {"x": {"avg_edit_distance": 0.95, "n": 1}}}}
    assert draft_quality_case_weights(summary)["x"] == 2.9
    summary["outcome"]["avg_edit_distance_by_sender_type"]["x"]["avg_edit_distance"] = 1.0
    assert draft_quality_case_weights(summary)["x"] == 3.0  # clamped


def test_case_weights_empty_or_dataless():
    assert draft_quality_case_weights({}) == {}
    assert draft_quality_case_weights({"outcome": {"avg_edit_distance_by_sender_type": {}}}) == {}
    assert draft_quality_case_weights("nope") == {}
    # cohort with no edit-distance value is skipped
    assert draft_quality_case_weights({"outcome": {"avg_edit_distance_by_sender_type": {"x": {"n": 3}}}}) == {}


# --- weighted scoring ------------------------------------------------------

# 2 passing "internal" cases + 2 failing "external_client" cases.
_CASES = [
    ("internal", "pass", 1.0, 1.0),
    ("internal", "pass", 1.0, 1.0),
    ("external_client", "fail", 0.0, 0.0),
    ("external_client", "fail", 0.0, 0.0),
]


def test_unweighted_is_equal_average(tmp_path):
    sc = scorecard_from_eval_result(_suite(_CASES), tmp_path)  # no autoresearch.yaml → default composite weights
    assert sc.composite == 0.5  # 0.5*0.5 + 0.3*0.5 + 0.2*0.5


def test_weighting_failing_cohort_lowers_composite(tmp_path):
    sc = scorecard_from_eval_result(_suite(_CASES), tmp_path, case_weights={"external_client": 3.0})
    assert sc.composite == 0.25  # the failing cohort now counts 3x → composite drops


def test_weighting_passing_cohort_raises_composite(tmp_path):
    sc = scorecard_from_eval_result(_suite(_CASES), tmp_path, case_weights={"internal": 3.0})
    assert sc.composite == 0.75


def test_uniform_weights_match_unweighted(tmp_path):
    uniform = scorecard_from_eval_result(_suite(_CASES), tmp_path, case_weights={"internal": 1.0, "external_client": 1.0})
    plain = scorecard_from_eval_result(_suite(_CASES), tmp_path)
    assert uniform.composite == plain.composite == 0.5


def test_unknown_category_defaults_to_weight_one(tmp_path):
    # weights only mention a category not present → behaves like uniform
    sc = scorecard_from_eval_result(_suite(_CASES), tmp_path, case_weights={"nonexistent": 5.0})
    assert sc.composite == 0.5


# --- gate ------------------------------------------------------------------


def test_weighting_disabled_by_default(tmp_path):
    assert draft_quality_weighting_enabled(tmp_path) is False  # no config file


def test_weighting_gate_reads_config(tmp_path):
    (tmp_path / "autoresearch.yaml").write_text("draft_quality_weighting: true\n", encoding="utf-8")
    assert draft_quality_weighting_enabled(tmp_path) is True
    (tmp_path / "autoresearch.yaml").write_text("composite_weights:\n  pass_rate: 0.5\n", encoding="utf-8")
    assert draft_quality_weighting_enabled(tmp_path) is False  # key absent → default off
