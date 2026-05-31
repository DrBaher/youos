"""Autoresearch optimization loop for YouOS."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.autoresearch.mutator import (
    ConfigSurface,
    apply_mutation,
    describe_mutation,
    get_mutable_surfaces,
    revert_mutation,
)
from app.autoresearch.run_log import ensure_table, log_iteration
from app.autoresearch.scorer import (
    Scorecard,
    compare_scorecards,
    draft_quality_case_weights,
    draft_quality_weighting_enabled,
    load_compare_thresholds,
    scorecard_from_eval_result,
)
from app.evaluation.service import EvalRequest, run_eval_suite

logger = logging.getLogger(__name__)


@dataclass
class IterationResult:
    iteration: int
    surface_name: str
    mutation_desc: str
    baseline_composite: float
    candidate_composite: float
    outcome: str  # "improved" | "neutral" | "regressed"
    kept: bool
    # Per-component baseline→candidate scores, so the run log shows WHICH signal
    # moved (or didn't) — the difference between "mutation had no effect on the
    # eval at all" (a config-application bug) and "moved but below threshold".
    baseline_pass: float = 0.0
    candidate_pass: float = 0.0
    baseline_kw: float = 0.0
    candidate_kw: float = 0.0
    baseline_conf: float = 0.0
    candidate_conf: float = 0.0


@dataclass
class AutoresearchReport:
    run_tag: str
    started_at: str
    baseline: Scorecard
    final: Scorecard
    iterations: list[IterationResult] = field(default_factory=list)
    total_eval_runs: int = 0
    improvements_kept: int = 0
    reverted: int = 0


def run_autoresearch(
    configs_dir: Path,
    database_url: str,
    *,
    generate_fn: Any,
    max_iterations: int = 10,
    baseline_tag: str = "autoresearch_baseline",
    dry_run: bool = False,
    surface_filter: str | None = None,
) -> AutoresearchReport:
    """Run the autoresearch optimization loop.

    Args:
        configs_dir: Path to configs/ directory.
        database_url: SQLite database URL.
        generate_fn: Generation function matching the eval runner interface.
        max_iterations: Max total eval runs (including baseline).
        baseline_tag: Config tag for the baseline run.
        dry_run: If True, show plan without executing.
        surface_filter: Optional "retrieval" or "prompt_drafting" to limit scope.
    """
    run_tag = f"autoresearch_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    started_at = datetime.now(timezone.utc).isoformat()

    surfaces = get_mutable_surfaces(configs_dir, surface_filter=surface_filter)

    if dry_run:
        return _dry_run_report(surfaces, run_tag, started_at)

    # Ensure logging table exists
    ensure_table(database_url)

    # Keep/revert thresholds (configurable; defaults 0.01). Loaded once so every
    # iteration this run is judged on the same bar.
    improve_threshold, regress_threshold = load_compare_thresholds(configs_dir)

    # Draft-quality case weights (default off): computed ONCE from the current
    # draft_events log and applied to every scorecard this run, so baseline and
    # candidates stay comparable. Pushes the objective toward the cohorts where
    # real drafts get edited most. No data / disabled → None → equal weighting.
    case_weights: dict[str, float] | None = None
    try:
        if draft_quality_weighting_enabled(configs_dir):
            from app.core.stats import summarize_draft_events

            case_weights = draft_quality_case_weights(summarize_draft_events(database_url)) or None
    except Exception:
        case_weights = None

    # 1. Establish baseline
    baseline_result = run_eval_suite(
        EvalRequest(config_tag=f"{run_tag}_baseline"),
        generate_fn=generate_fn,
        database_url=database_url,
        configs_dir=configs_dir,
        persist=True,
    )
    baseline = scorecard_from_eval_result(baseline_result, configs_dir, case_weights=case_weights)
    eval_count = 1
    current_baseline = baseline

    report = AutoresearchReport(
        run_tag=run_tag,
        started_at=started_at,
        baseline=baseline,
        final=baseline,
    )

    # 2. Iterate over surfaces
    for surface in surfaces:
        if eval_count >= max_iterations:
            break

        mutation_desc = describe_mutation(surface)

        # Skip if at boundary
        if "at boundary" in mutation_desc:
            continue

        old_value = apply_mutation(surface, configs_dir)
        if old_value == surface.current_value:
            # No actual change (boundary)
            continue

        try:
            # Run eval with mutated config
            candidate_result = run_eval_suite(
                EvalRequest(config_tag=f"{run_tag}_iter{eval_count}"),
                generate_fn=generate_fn,
                database_url=database_url,
                configs_dir=configs_dir,
                persist=True,
            )
            eval_count += 1
            candidate = scorecard_from_eval_result(candidate_result, configs_dir, case_weights=case_weights)
            outcome = compare_scorecards(
                current_baseline, candidate,
                improve_threshold=improve_threshold, regress_threshold=regress_threshold,
            )

            kept = outcome == "improved"
            if not kept:
                revert_mutation(surface, old_value, configs_dir)

            iteration = IterationResult(
                iteration=eval_count - 1,
                surface_name=surface.name,
                mutation_desc=mutation_desc,
                baseline_composite=current_baseline.composite,
                candidate_composite=candidate.composite,
                outcome=outcome,
                kept=kept,
                baseline_pass=current_baseline.pass_rate,
                candidate_pass=candidate.pass_rate,
                baseline_kw=current_baseline.avg_keyword_hit,
                candidate_kw=candidate.avg_keyword_hit,
                baseline_conf=current_baseline.avg_confidence,
                candidate_conf=candidate.avg_confidence,
            )
            report.iterations.append(iteration)

            log_iteration(
                database_url,
                run_tag=run_tag,
                iteration=eval_count - 1,
                surface_name=surface.name,
                mutation_desc=mutation_desc,
                baseline_composite=current_baseline.composite,
                candidate_composite=candidate.composite,
                outcome=outcome,
                kept=kept,
            )

            if kept:
                current_baseline = candidate
                report.improvements_kept += 1
            else:
                report.reverted += 1
        except Exception as exc:
            # A transient eval failure must NOT leave the mutated (unvalidated)
            # config live or abort the rest of the run — revert and move on.
            logger.warning("autoresearch: surface %s failed mid-eval, reverting: %s", surface.name, exc)
            try:
                revert_mutation(surface, old_value, configs_dir)
            except Exception:
                pass
            continue

    report.total_eval_runs = eval_count
    report.final = current_baseline

    # Write structured JSON run log
    _write_jsonl_entry(report, configs_dir)

    return report


def _write_jsonl_entry(report: AutoresearchReport, configs_dir: Path) -> None:
    """Append a JSON line to var/autoresearch_runs.jsonl."""
    root = configs_dir.parent
    jsonl_path = root / "var" / "autoresearch_runs.jsonl"
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    improvements = [it.surface_name for it in report.iterations if it.kept]
    regressions = [it.surface_name for it in report.iterations if it.outcome == "regressed"]

    entry = {
        "run_at": report.started_at,
        "iterations": report.total_eval_runs,
        "composite_score": report.final.composite,
        "improvements": improvements,
        "regressions": regressions,
        "config_snapshot": {
            "baseline_composite": report.baseline.composite,
            "final_composite": report.final.composite,
            "improvements_kept": report.improvements_kept,
            "reverted": report.reverted,
        },
        # Per-iteration component deltas — so a flat run is diagnosable after the
        # fact (did pass/kw/conf actually respond to each mutation?).
        "iteration_components": [
            {
                "surface": it.surface_name,
                "outcome": it.outcome,
                "composite": [it.baseline_composite, it.candidate_composite],
                "pass": [it.baseline_pass, it.candidate_pass],
                "kw": [it.baseline_kw, it.candidate_kw],
                "conf": [it.baseline_conf, it.candidate_conf],
            }
            for it in report.iterations
        ],
    }
    with open(jsonl_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    _trim_jsonl(jsonl_path, _AUTORESEARCH_JSONL_MAX_LINES)


# Keep the append-only run log bounded — stats.py reads only the last few lines,
# so an unbounded file is pure waste (b162).
_AUTORESEARCH_JSONL_MAX_LINES = 365


def _trim_jsonl(path: Path, max_lines: int) -> None:
    """Atomically keep only the last ``max_lines`` of an append-only jsonl file
    (O_EXCL-free temp + os.replace, matching the data_safety atomic-write style)."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= max_lines:
            return
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines[-max_lines:])
        os.replace(tmp, path)
    except OSError:
        pass


def _dry_run_report(
    surfaces: list[ConfigSurface],
    run_tag: str,
    started_at: str,
) -> AutoresearchReport:
    """Build a report showing what would be mutated without doing it."""
    dummy = Scorecard(
        config_tag="dry_run",
        pass_rate=0.0,
        warn_rate=0.0,
        fail_rate=0.0,
        avg_keyword_hit=0.0,
        avg_confidence=0.0,
        composite=0.0,
    )
    report = AutoresearchReport(
        run_tag=run_tag,
        started_at=started_at,
        baseline=dummy,
        final=dummy,
    )
    for surface in surfaces:
        desc = describe_mutation(surface)
        report.iterations.append(
            IterationResult(
                iteration=0,
                surface_name=surface.name,
                mutation_desc=desc,
                baseline_composite=0.0,
                candidate_composite=0.0,
                outcome="dry_run",
                kept=False,
            )
        )
    return report


def format_report(report: AutoresearchReport) -> str:
    """Format an autoresearch report for terminal output."""
    lines: list[str] = []
    lines.append(f"YouOS Autoresearch — {report.started_at}")
    lines.append("━" * 50)

    if report.baseline.composite > 0 or report.final.composite > 0:
        lines.append(f"Baseline: {report.baseline.summary()}")
        lines.append("")

    def _components(it: IterationResult) -> str:
        # Show which signal moved so a flat composite is diagnosable: if these
        # are all identical, the mutation didn't affect the eval (config not
        # applied?) rather than just moving below threshold.
        return (
            f"    pass {it.baseline_pass:.2f}->{it.candidate_pass:.2f}  "
            f"kw {it.baseline_kw:.2f}->{it.candidate_kw:.2f}  "
            f"conf {it.baseline_conf:.2f}->{it.candidate_conf:.2f}"
        )

    _LABEL = {"improved": ("Improved", "keeping"), "neutral": ("Neutral", "reverting"),
              "regressed": ("Regressed", "reverting")}
    for it in report.iterations:
        prefix = f"[{it.iteration}/{report.total_eval_runs or len(report.iterations)}]"
        if it.outcome == "dry_run":
            lines.append(f"  {it.mutation_desc}")
            continue
        label, verb = _LABEL.get(it.outcome, (it.outcome, "reverting"))
        delta = f"composite {it.baseline_composite:.3f} -> {it.candidate_composite:.3f}"
        lines.append(
            f"{prefix} Mutating {it.mutation_desc}\n"
            f"  {label}: {delta} — {verb}\n{_components(it)}"
        )

    lines.append("━" * 50)

    if report.total_eval_runs > 0:
        lines.append(f"Final: {report.final.summary()}")
        lines.append(f"Improvements kept: {report.improvements_kept} | Reverted: {report.reverted} | Iterations: {report.total_eval_runs}")
    else:
        lines.append(f"Dry run: {len(report.iterations)} surfaces would be mutated")

    return "\n".join(lines)
