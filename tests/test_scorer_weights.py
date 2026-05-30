"""Tests for configurable autoresearch composite weights (Item 10)."""

from __future__ import annotations

import yaml

from app.autoresearch.mutator import (
    _normalize_composite_weights,
    get_mutable_surfaces,
)
from app.autoresearch.scorer import (
    _DEFAULT_WEIGHTS,
    load_composite_weights,
    reset_weight_cache,
    scorecard_from_eval_result,
)
from app.evaluation.service import CaseResult, EvalSuiteResult


def _case(pass_fail: str) -> CaseResult:
    return CaseResult(
        case_key="k", category="internal", prompt_text="p", draft="d",
        detected_mode="internal", confidence="medium", precedent_count=1,
        scores={"keyword_hit_rate": 0.0, "confidence_score": 0.0},
        pass_fail=pass_fail, notes="",
    )


def _suite(cases):
    return EvalSuiteResult(
        config_tag="t", total_cases=len(cases),
        passed=sum(1 for c in cases if c.pass_fail == "pass"),
        warned=sum(1 for c in cases if c.pass_fail == "warn"),
        failed=sum(1 for c in cases if c.pass_fail == "fail"),
        case_results=cases, run_at="2026-05-29",
    )


def test_warn_cases_get_half_credit_in_composite(tmp_path):
    """A 'warn' case contributes half the pass-term credit (graded), so the
    optimizer can see incremental progress instead of a binary cliff."""
    reset_weight_cache()
    # Two warns, zero kw/conf. graded_pass = 0 + 0.5*1.0 = 0.5;
    # composite = pass_weight(0.5) * 0.5 = 0.25 (was 0.0 with binary scoring).
    sc = scorecard_from_eval_result(_suite([_case("warn"), _case("warn")]), tmp_path)
    assert sc.pass_rate == 0.0          # display stays strict
    assert abs(sc.composite - 0.25) < 1e-6
    reset_weight_cache()


def test_fail_cases_get_no_credit(tmp_path):
    reset_weight_cache()
    sc = scorecard_from_eval_result(_suite([_case("fail"), _case("fail")]), tmp_path)
    assert sc.composite == 0.0
    reset_weight_cache()


def test_default_weights():
    """Default weights sum to 1.0."""
    assert sum(_DEFAULT_WEIGHTS.values()) == 1.0


def test_load_composite_weights_from_config(tmp_path):
    """Loads weights from autoresearch.yaml."""
    reset_weight_cache()
    config_path = tmp_path / "autoresearch.yaml"
    config_path.write_text(yaml.dump({"composite_weights": {"pass_rate": 0.6, "avg_keyword_hit": 0.2, "avg_confidence": 0.2}}))
    weights = load_composite_weights(tmp_path)
    assert weights["pass_rate"] == 0.6
    assert weights["avg_keyword_hit"] == 0.2
    reset_weight_cache()


def test_load_composite_weights_missing_file(tmp_path):
    """Falls back to defaults when config file is missing."""
    reset_weight_cache()
    weights = load_composite_weights(tmp_path)
    assert weights == _DEFAULT_WEIGHTS
    reset_weight_cache()


def test_normalize_composite_weights():
    """Weights are normalized to sum to 1.0."""
    data = {"composite_weights": {"pass_rate": 0.6, "avg_keyword_hit": 0.3, "avg_confidence": 0.3}}
    _normalize_composite_weights(data)
    total = sum(data["composite_weights"].values())
    assert abs(total - 1.0) < 0.01


def test_get_mutable_surfaces_includes_composite(tmp_path):
    """Composite weight surfaces appear in mutable surfaces."""
    # Create minimal config files
    (tmp_path / "autoresearch.yaml").write_text(yaml.dump({"composite_weights": {"pass_rate": 0.5, "avg_keyword_hit": 0.3, "avg_confidence": 0.2}}))
    (tmp_path / "retrieval").mkdir()
    (tmp_path / "retrieval" / "defaults.yaml").write_text(
        yaml.dump(
            {
                "top_k_reply_pairs": 5,
                "top_k_chunks": 3,
                "recency_boost_days": 90,
                "recency_boost_weight": 0.2,
                "account_boost_weight": 0.15,
            }
        )
    )
    (tmp_path / "prompts.yaml").write_text(yaml.dump({"drafting_prompt": "test"}))

    surfaces = get_mutable_surfaces(tmp_path, surface_filter="autoresearch")
    names = [s.name for s in surfaces]
    assert "composite_weight_pass_rate" in names
    assert "composite_weight_avg_keyword_hit" in names
    assert "composite_weight_avg_confidence" in names

    # Check bounds
    for s in surfaces:
        assert s.step_size == 0.05
        assert s.min_val == 0.0
        assert s.max_val == 1.0


def _sc(composite: float):
    from app.autoresearch.scorer import Scorecard
    return Scorecard(config_tag="t", pass_rate=0.0, warn_rate=0.0, fail_rate=0.0,
                     avg_keyword_hit=0.0, avg_confidence=0.0, composite=composite)


def test_compare_keeps_small_real_improvement():
    """A +0.01 composite gain (a few keyword points — the magnitude real prompt
    wins land at) is now 'improved', not discarded by the old +0.02 bar."""
    from app.autoresearch.scorer import compare_scorecards
    assert compare_scorecards(_sc(0.453), _sc(0.463)) == "improved"   # +0.010
    assert compare_scorecards(_sc(0.45), _sc(0.455)) == "neutral"     # +0.005 below bar
    assert compare_scorecards(_sc(0.45), _sc(0.43)) == "regressed"    # -0.020


def test_compare_thresholds_are_configurable():
    from app.autoresearch.scorer import compare_scorecards
    # Raise the bar → the same +0.012 is no longer enough.
    assert compare_scorecards(_sc(0.45), _sc(0.462), improve_threshold=0.03) == "neutral"


def test_load_compare_thresholds_from_config(tmp_path):
    import yaml as _yaml

    from app.autoresearch.scorer import load_compare_thresholds
    (tmp_path / "autoresearch.yaml").write_text(_yaml.dump({"improve_threshold": 0.04, "regress_threshold": 0.02}))
    assert load_compare_thresholds(tmp_path) == (0.04, 0.02)
    # Missing file → defaults.
    assert load_compare_thresholds(tmp_path / "nope") == (0.01, 0.01)


def test_composite_weight_mutate_then_revert_restores_exact_original(tmp_path):
    """b147: normalization rewrites all three weights, so revert must restore the
    WHOLE dict — restoring only the mutated key left the rest skewed (sum != 1),
    silently corrupting the eval objective over successive nightly runs."""
    from app.autoresearch.mutator import apply_mutation, get_mutable_surfaces, revert_mutation

    orig = {"pass_rate": 0.5, "avg_keyword_hit": 0.3, "avg_confidence": 0.2}
    (tmp_path / "autoresearch.yaml").write_text(yaml.safe_dump({"composite_weights": dict(orig)}))
    (tmp_path / "retrieval").mkdir()
    (tmp_path / "retrieval" / "defaults.yaml").write_text("top_k_reply_pairs: 5\n")
    (tmp_path / "prompts.yaml").write_text("system_prompt_suffix: ''\n")
    (tmp_path / "persona.yaml").write_text("modes: {}\n")

    comp = next(s for s in get_mutable_surfaces(tmp_path)
                if s.config_file == "autoresearch.yaml" and "composite_weights" in s.yaml_key)
    old = apply_mutation(comp, tmp_path)
    mutated = yaml.safe_load((tmp_path / "autoresearch.yaml").read_text())["composite_weights"]
    assert mutated != orig and abs(sum(mutated.values()) - 1.0) < 1e-9

    revert_mutation(comp, old, tmp_path)
    restored = yaml.safe_load((tmp_path / "autoresearch.yaml").read_text())["composite_weights"]
    assert restored == orig  # exactly the operator-set 0.5/0.3/0.2


def test_load_composite_weights_renormalizes_skewed_file(tmp_path):
    """b147: any on-disk drift (a historically-buggy revert) is corrected to
    sum 1.0 on load so it can't bias scoring."""
    (tmp_path / "autoresearch.yaml").write_text(
        yaml.safe_dump({"composite_weights": {"pass_rate": 0.5, "avg_keyword_hit": 0.2857, "avg_confidence": 0.1905}})
    )
    reset_weight_cache()
    w = load_composite_weights(tmp_path)
    assert abs(sum(w.values()) - 1.0) < 1e-6
