import json
import sqlite3
from pathlib import Path
from typing import Any

from app.evaluation.service import (
    CaseResult,
    EvalRequest,
    EvalSuiteResult,
    compute_pass_fail,
    confidence_to_score,
    evaluate_case,
    persist_case_result,
    run_eval_suite,
    score_brevity,
    score_keyword_hit_rate,
    score_mode_match,
)
from scripts.seed_benchmarks import load_cases, seed_benchmarks

ROOT_DIR = Path(__file__).resolve().parents[1]


def _make_db(db_path: Path) -> None:
    schema_sql = (ROOT_DIR / "docs" / "schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        conn.commit()
    finally:
        conn.close()


# ── Seeder tests ─────────────────────────────────────────────────────

def test_load_cases_from_fixtures() -> None:
    cases = load_cases(ROOT_DIR / "fixtures" / "benchmark_cases.yaml")
    assert len(cases) == 15
    keys = [c["case_key"] for c in cases]
    assert "work_pricing_quote" in keys
    assert "personal_meetup" in keys
    assert "mixed_short_oneliner" in keys


def test_seed_benchmarks_inserts(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _make_db(db_path)
    cases = load_cases(ROOT_DIR / "fixtures" / "benchmark_cases.yaml")
    result = seed_benchmarks(cases, db_path)
    assert result["inserted"] == 15
    assert result["updated"] == 0
    assert result["total"] == 15

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM benchmark_cases").fetchone()[0]
    conn.close()
    assert count == 15


def test_seed_benchmarks_upserts(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _make_db(db_path)
    cases = load_cases(ROOT_DIR / "fixtures" / "benchmark_cases.yaml")
    seed_benchmarks(cases, db_path)
    result = seed_benchmarks(cases, db_path)
    assert result["inserted"] == 0
    assert result["updated"] == 15

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM benchmark_cases").fetchone()[0]
    conn.close()
    assert count == 15


def test_seeded_cases_have_expected_properties(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _make_db(db_path)
    cases = load_cases(ROOT_DIR / "fixtures" / "benchmark_cases.yaml")
    seed_benchmarks(cases, db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM benchmark_cases WHERE case_key = ?", ("work_pricing_quote",)
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["category"] == "work"
    props = json.loads(row["expected_properties_json"])
    assert props["mode"] == "work"
    assert "pricing" in props["should_contain_keywords"]


# ── Scoring logic tests ──────────────────────────────────────────────

def test_keyword_hit_rate_all_match() -> None:
    assert score_keyword_hit_rate("Here is the pricing and details", ["pricing", "details"]) == 1.0


def test_keyword_hit_rate_partial() -> None:
    assert score_keyword_hit_rate("Here is the pricing", ["pricing", "details"]) == 0.5


def test_keyword_hit_rate_none_match() -> None:
    assert score_keyword_hit_rate("Hello world", ["pricing", "details"]) == 0.0


def test_keyword_hit_rate_empty_keywords() -> None:
    assert score_keyword_hit_rate("anything", []) == 1.0


def test_keyword_hit_rate_case_insensitive() -> None:
    assert score_keyword_hit_rate("PRICING and Details", ["pricing", "details"]) == 1.0


def test_brevity_pass() -> None:
    assert score_brevity(80, 100) == "pass"


def test_brevity_at_limit() -> None:
    assert score_brevity(100, 100) == "pass"


def test_brevity_warn() -> None:
    assert score_brevity(140, 100) == "warn"


def test_brevity_fail() -> None:
    assert score_brevity(160, 100) == "fail"


def test_brevity_no_limit() -> None:
    assert score_brevity(9999, None) == "pass"


def test_mode_match_true() -> None:
    assert score_mode_match("work", "work") is True


def test_mode_match_false() -> None:
    assert score_mode_match("personal", "work") is False


def test_mode_match_case_insensitive() -> None:
    assert score_mode_match("Work", "work") is True


def test_confidence_to_score_values() -> None:
    assert confidence_to_score("high") == 1.0
    assert confidence_to_score("medium") == 0.5
    assert confidence_to_score("low") == 0.0
    assert confidence_to_score("unknown") == 0.0


def test_compute_pass_fail_all_good() -> None:
    assert compute_pass_fail(1.0, "pass", True) == "pass"


def test_compute_pass_fail_brevity_fail() -> None:
    assert compute_pass_fail(1.0, "fail", True) == "fail"


def test_compute_pass_fail_mode_mismatch() -> None:
    assert compute_pass_fail(1.0, "pass", False) == "fail"


def test_compute_pass_fail_low_keywords() -> None:
    assert compute_pass_fail(0.3, "pass", True) == "fail"


def test_compute_pass_fail_warn_brevity() -> None:
    assert compute_pass_fail(1.0, "warn", True) == "warn"


def test_compute_pass_fail_borderline_keywords() -> None:
    assert compute_pass_fail(0.6, "pass", True) == "warn"


# ── evaluate_case integration ────────────────────────────────────────

def test_evaluate_case_pass() -> None:
    case = {
        "case_key": "test_case",
        "category": "work",
        "prompt_text": "Send pricing",
        "expected_properties": {
            "mode": "work",
            "should_contain_keywords": ["pricing", "details"],
            "max_words": 150,
        },
        "notes": "test",
    }
    cr = evaluate_case(
        case=case,
        draft="Here is the pricing with all the details you need.",
        detected_mode="work",
        confidence="high",
        precedent_count=3,
    )
    assert cr.pass_fail == "pass"
    assert cr.scores["keyword_hit_rate"] == 1.0
    assert cr.scores["brevity_fit"] == "pass"
    assert cr.scores["mode_match"] is True


def test_evaluate_case_fail_mode() -> None:
    case = {
        "case_key": "test_case",
        "category": "work",
        "prompt_text": "Send pricing",
        "expected_properties": {"mode": "work", "should_contain_keywords": [], "max_words": 150},
        "notes": "",
    }
    cr = evaluate_case(
        case=case,
        draft="Sure thing buddy.",
        detected_mode="personal",
        confidence="low",
        precedent_count=0,
    )
    assert cr.pass_fail == "fail"


# ── EvalSuiteResult structure ────────────────────────────────────────

def test_eval_suite_result_to_dict() -> None:
    cr = CaseResult(
        case_key="test",
        category="work",
        prompt_text="hi",
        draft="hello",
        detected_mode="work",
        confidence="high",
        precedent_count=2,
        scores={"keyword_hit_rate": 1.0},
        pass_fail="pass",
        notes="",
    )
    suite = EvalSuiteResult(
        config_tag="v1",
        total_cases=1,
        passed=1,
        warned=0,
        failed=0,
        case_results=[cr],
        run_at="2026-03-15T00:00:00Z",
    )
    d = suite.to_dict()
    assert d["config_tag"] == "v1"
    assert d["total_cases"] == 1
    assert len(d["case_results"]) == 1
    assert d["case_results"][0]["case_key"] == "test"


# ── Persistence test ─────────────────────────────────────────────────

def test_persist_case_result(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _make_db(db_path)
    cr = CaseResult(
        case_key="test_persist",
        category="work",
        prompt_text="hi",
        draft="hello",
        detected_mode="work",
        confidence="high",
        precedent_count=2,
        scores={"keyword_hit_rate": 1.0, "brevity_fit": "pass"},
        pass_fail="pass",
        notes="",
    )
    conn = sqlite3.connect(db_path)
    try:
        persist_case_result(conn, cr, "v1_test", None)
        conn.commit()
        row = conn.execute("SELECT * FROM eval_runs").fetchone()
    finally:
        conn.close()
    assert row is not None


# ── Full suite run with mock generation ──────────────────────────────

def _mock_generate(prompt_text: str, *, database_url: str, configs_dir: Path) -> dict[str, Any]:
    return {
        "draft": "Here is the pricing with details. Happy to help.",
        "detected_mode": "work",
        "confidence": "high",
        "precedent_count": 3,
    }


def test_run_eval_suite_full(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _make_db(db_path)

    # Seed two cases
    conn = sqlite3.connect(db_path)
    for key, cat in [("case_a", "work"), ("case_b", "personal")]:
        props = json.dumps({
            "mode": cat,
            "should_contain_keywords": ["pricing", "details"],
            "max_words": 150,
        })
        conn.execute(
            "INSERT INTO benchmark_cases (case_key, category, prompt_text, expected_properties_json) "
            "VALUES (?, ?, ?, ?)",
            (key, cat, f"Test prompt for {key}", props),
        )
    conn.commit()
    conn.close()

    result = run_eval_suite(
        EvalRequest(config_tag="test_run"),
        generate_fn=_mock_generate,
        database_url=f"sqlite:///{db_path}",
        configs_dir=ROOT_DIR / "configs",
        persist=True,
    )

    assert result.total_cases == 2
    assert result.passed + result.warned + result.failed == 2
    assert result.config_tag == "test_run"

    # Verify persistence
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM eval_runs").fetchone()[0]
    conn.close()
    assert count == 2


def test_run_eval_suite_single_case(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _make_db(db_path)

    conn = sqlite3.connect(db_path)
    props = json.dumps({"mode": "work", "should_contain_keywords": ["pricing"], "max_words": 150})
    conn.execute(
        "INSERT INTO benchmark_cases (case_key, category, prompt_text, expected_properties_json) "
        "VALUES (?, ?, ?, ?)",
        ("only_case", "work", "Test prompt", props),
    )
    conn.commit()
    conn.close()

    result = run_eval_suite(
        EvalRequest(case_key="only_case", config_tag="single"),
        generate_fn=_mock_generate,
        database_url=f"sqlite:///{db_path}",
        configs_dir=ROOT_DIR / "configs",
        persist=False,
    )
    assert result.total_cases == 1
    assert result.case_results[0].case_key == "only_case"


def test_run_eval_suite_no_persist(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _make_db(db_path)

    conn = sqlite3.connect(db_path)
    props = json.dumps({"mode": "work", "should_contain_keywords": [], "max_words": 100})
    conn.execute(
        "INSERT INTO benchmark_cases (case_key, category, prompt_text, expected_properties_json) "
        "VALUES (?, ?, ?, ?)",
        ("np_case", "work", "No persist test", props),
    )
    conn.commit()
    conn.close()

    run_eval_suite(
        EvalRequest(config_tag="np"),
        generate_fn=_mock_generate,
        database_url=f"sqlite:///{db_path}",
        configs_dir=ROOT_DIR / "configs",
        persist=False,
    )

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM eval_runs").fetchone()[0]
    conn.close()
    assert count == 0
