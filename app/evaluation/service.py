"""Evaluation service — rule-based scoring for YouOS benchmark cases."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class EvalRequest:
    case_key: str | None = None  # run specific case; None = run all
    config_tag: str = "default"  # label for this run set


@dataclass
class CaseResult:
    case_key: str
    category: str
    prompt_text: str
    draft: str
    detected_mode: str
    confidence: str
    precedent_count: int
    scores: dict[str, Any]
    pass_fail: str  # "pass" | "fail" | "warn"
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvalSuiteResult:
    config_tag: str
    total_cases: int
    passed: int
    warned: int
    failed: int
    case_results: list[CaseResult]
    run_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **{k: v for k, v in asdict(self).items() if k != "case_results"},
            "case_results": [cr.to_dict() for cr in self.case_results],
        }


# -- Scoring helpers (deterministic, rule-based) ──────────────────────


def score_keyword_hit_rate(draft: str, keywords: list[str]) -> float:
    if not keywords:
        return 1.0
    draft_lower = draft.lower()
    hits = sum(1 for kw in keywords if kw.lower() in draft_lower)
    return hits / len(keywords)


def score_brevity(word_count: int, max_words: int | None) -> str:
    if max_words is None:
        return "pass"
    if word_count <= max_words:
        return "pass"
    if word_count <= max_words * 1.1:
        return "warn"
    if word_count <= max_words * 1.5:
        return "warn"
    return "fail"


def score_mode_match(detected_mode: str, expected_mode: str) -> bool:
    return detected_mode.lower() == expected_mode.lower()


def confidence_to_score(confidence: str) -> float:
    return {"high": 1.0, "medium": 0.5, "low": 0.0}.get(confidence.lower(), 0.0)


def compute_pass_fail(
    keyword_hit_rate: float,
    brevity_fit: str,
    mode_match: bool,
) -> str:
    if brevity_fit == "fail":
        return "fail"
    if not mode_match:
        return "fail"
    if keyword_hit_rate < 0.5:
        return "fail"
    if brevity_fit == "warn" or keyword_hit_rate < 0.8:
        return "warn"
    return "pass"


# -- Case evaluation ──────────────────────────────────────────────────


def evaluate_case(
    case: dict[str, Any],
    draft: str,
    detected_mode: str,
    confidence: str,
    precedent_count: int,
) -> CaseResult:
    expected = case.get("expected_properties", {})
    if isinstance(expected, str):
        expected = json.loads(expected)

    keywords = expected.get("should_contain_keywords", [])
    max_words = expected.get("max_words")
    expected_mode = expected.get("mode", "")

    word_count = len(draft.split())
    kw_rate = score_keyword_hit_rate(draft, keywords)
    brevity = score_brevity(word_count, max_words)
    mode_ok = score_mode_match(detected_mode, expected_mode)
    conf_score = confidence_to_score(confidence)

    pf = compute_pass_fail(kw_rate, brevity, mode_ok)

    scores = {
        "keyword_hit_rate": round(kw_rate, 2),
        "brevity_fit": brevity,
        "mode_match": mode_ok,
        "confidence_score": conf_score,
        "word_count": word_count,
        "max_words": max_words,
    }

    return CaseResult(
        case_key=case["case_key"],
        category=case["category"],
        prompt_text=case["prompt_text"],
        draft=draft,
        detected_mode=detected_mode,
        confidence=confidence,
        precedent_count=precedent_count,
        scores=scores,
        pass_fail=pf,
        notes=case.get("notes", ""),
    )


# -- Persistence ──────────────────────────────────────────────────────


def persist_case_result(
    conn: sqlite3.Connection,
    case_result: CaseResult,
    config_tag: str,
    benchmark_case_id: int | None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    run_key = f"{config_tag}_{case_result.case_key}_{uuid.uuid4().hex[:8]}"
    conn.execute(
        """
        INSERT INTO eval_runs
            (run_key, benchmark_case_id, config_snapshot_json,
             retrieval_summary_json, generation_output, score_json,
             status, started_at, completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_key,
            benchmark_case_id,
            json.dumps({"config_tag": config_tag}),
            json.dumps({"precedent_count": case_result.precedent_count}),
            case_result.draft,
            json.dumps(case_result.scores),
            case_result.pass_fail,
            now,
            now,
        ),
    )


# -- Suite runner ─────────────────────────────────────────────────────


def load_benchmark_cases(conn: sqlite3.Connection, case_key: str | None = None) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    if case_key:
        rows = conn.execute("SELECT * FROM benchmark_cases WHERE case_key = ?", (case_key,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM benchmark_cases").fetchall()
    return [dict(r) for r in rows]


def run_eval_suite(
    request: EvalRequest,
    *,
    generate_fn: Any,
    database_url: str,
    configs_dir: Path,
    persist: bool = True,
) -> EvalSuiteResult:
    from app.db.bootstrap import resolve_sqlite_path

    db_path = resolve_sqlite_path(database_url)
    conn = sqlite3.connect(db_path)
    try:
        cases = load_benchmark_cases(conn, request.case_key)
        case_results: list[CaseResult] = []

        for case in cases:
            expected = case.get("expected_properties_json", "{}")
            if isinstance(expected, str):
                expected_props = json.loads(expected)
            else:
                expected_props = expected

            # Build a dict with parsed expected_properties for scoring
            eval_case = {
                "case_key": case["case_key"],
                "category": case["category"],
                "prompt_text": case["prompt_text"],
                "expected_properties": expected_props,
                "notes": case.get("notes", ""),
            }

            # Call generation
            draft_response = generate_fn(
                case["prompt_text"],
                database_url=database_url,
                configs_dir=configs_dir,
            )

            cr = evaluate_case(
                case=eval_case,
                draft=draft_response["draft"],
                detected_mode=draft_response["detected_mode"],
                confidence=draft_response["confidence"],
                precedent_count=draft_response["precedent_count"],
            )
            case_results.append(cr)

            if persist:
                persist_case_result(
                    conn,
                    cr,
                    request.config_tag,
                    case.get("id"),
                )

        if persist:
            conn.commit()
    finally:
        conn.close()

    passed = sum(1 for cr in case_results if cr.pass_fail == "pass")
    warned = sum(1 for cr in case_results if cr.pass_fail == "warn")
    failed = sum(1 for cr in case_results if cr.pass_fail == "fail")

    return EvalSuiteResult(
        config_tag=request.config_tag,
        total_cases=len(case_results),
        passed=passed,
        warned=warned,
        failed=failed,
        case_results=case_results,
        run_at=datetime.now(timezone.utc).isoformat(),
    )
