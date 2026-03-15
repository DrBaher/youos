"""Scorecard comparison for YouOS Autoresearch."""
from __future__ import annotations

from dataclasses import dataclass

from app.evaluation.service import EvalSuiteResult


@dataclass
class Scorecard:
    config_tag: str
    pass_rate: float       # passed / total
    warn_rate: float
    fail_rate: float
    avg_keyword_hit: float
    avg_confidence: float
    composite: float       # weighted: 0.5*pass_rate + 0.3*avg_keyword_hit + 0.2*avg_confidence

    def summary(self) -> str:
        return (
            f"pass={self.pass_rate:.0%} kw={self.avg_keyword_hit:.0%} "
            f"conf={self.avg_confidence:.0%} composite={self.composite:.2f}"
        )


def scorecard_from_eval_result(result: EvalSuiteResult) -> Scorecard:
    total = result.total_cases
    if total == 0:
        return Scorecard(
            config_tag=result.config_tag,
            pass_rate=0.0, warn_rate=0.0, fail_rate=0.0,
            avg_keyword_hit=0.0, avg_confidence=0.0, composite=0.0,
        )

    pass_rate = result.passed / total
    warn_rate = result.warned / total
    fail_rate = result.failed / total

    avg_kw = sum(
        cr.scores.get("keyword_hit_rate", 0.0) for cr in result.case_results
    ) / total
    avg_conf = sum(
        cr.scores.get("confidence_score", 0.0) for cr in result.case_results
    ) / total

    composite = 0.5 * pass_rate + 0.3 * avg_kw + 0.2 * avg_conf

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
