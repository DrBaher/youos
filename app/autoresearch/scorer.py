"""Scorecard comparison for YouOS Autoresearch."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.evaluation.service import EvalSuiteResult

_DEFAULT_WEIGHTS = {"pass_rate": 0.5, "avg_keyword_hit": 0.3, "avg_confidence": 0.2}
_cached_weights: dict[str, float] | None = None


def load_composite_weights(configs_dir: Path | None = None) -> dict[str, float]:
    """Load composite weights from configs/autoresearch.yaml, with caching."""
    global _cached_weights
    if _cached_weights is not None:
        return _cached_weights

    if configs_dir is None:
        configs_dir = Path(__file__).resolve().parents[2] / "configs"

    config_path = configs_dir / "autoresearch.yaml"
    if config_path.exists():
        import yaml

        try:
            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            weights = data.get("composite_weights", {})
            result = {
                "pass_rate": float(weights.get("pass_rate", _DEFAULT_WEIGHTS["pass_rate"])),
                "avg_keyword_hit": float(weights.get("avg_keyword_hit", _DEFAULT_WEIGHTS["avg_keyword_hit"])),
                "avg_confidence": float(weights.get("avg_confidence", _DEFAULT_WEIGHTS["avg_confidence"])),
            }
            _cached_weights = result
            return result
        except Exception:
            pass

    _cached_weights = dict(_DEFAULT_WEIGHTS)
    return _cached_weights


def reset_weight_cache() -> None:
    """Clear cached weights (for testing)."""
    global _cached_weights
    _cached_weights = None


@dataclass
class Scorecard:
    config_tag: str
    pass_rate: float  # passed / total
    warn_rate: float
    fail_rate: float
    avg_keyword_hit: float
    avg_confidence: float
    composite: float  # weighted: 0.5*pass_rate + 0.3*avg_keyword_hit + 0.2*avg_confidence

    def summary(self) -> str:
        return f"pass={self.pass_rate:.0%} kw={self.avg_keyword_hit:.0%} conf={self.avg_confidence:.0%} composite={self.composite:.2f}"


def scorecard_from_eval_result(result: EvalSuiteResult) -> Scorecard:
    total = result.total_cases
    if total == 0:
        return Scorecard(
            config_tag=result.config_tag,
            pass_rate=0.0,
            warn_rate=0.0,
            fail_rate=0.0,
            avg_keyword_hit=0.0,
            avg_confidence=0.0,
            composite=0.0,
        )

    pass_rate = result.passed / total
    warn_rate = result.warned / total
    fail_rate = result.failed / total

    avg_kw = sum(cr.scores.get("keyword_hit_rate", 0.0) for cr in result.case_results) / total
    avg_conf = sum(cr.scores.get("confidence_score", 0.0) for cr in result.case_results) / total

    weights = load_composite_weights()
    composite = weights["pass_rate"] * pass_rate + weights["avg_keyword_hit"] * avg_kw + weights["avg_confidence"] * avg_conf

    return Scorecard(
        config_tag=result.config_tag,
        pass_rate=round(pass_rate, 4),
        warn_rate=round(warn_rate, 4),
        fail_rate=round(fail_rate, 4),
        avg_keyword_hit=round(avg_kw, 4),
        avg_confidence=round(avg_conf, 4),
        composite=round(composite, 4),
    )


def compare_scorecards(baseline: Scorecard, candidate: Scorecard) -> str:
    """Compare two scorecards.

    Returns:
        "improved"  — composite >= baseline + 0.02
        "regressed" — composite < baseline - 0.01
        "neutral"   — otherwise
    """
    diff = candidate.composite - baseline.composite
    if diff >= 0.02:
        return "improved"
    if diff < -0.01:
        return "regressed"
    return "neutral"
