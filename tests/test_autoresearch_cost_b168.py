"""Tests for b168 — autoresearch cost + broken-fallback fixes.

Three independent levers, all hermetic (no real model, no network, temp dirs):

1. Restricted eval set: autoresearch scopes each eval suite to the stable
   golden subset (case_key LIKE 'golden-%') instead of the whole rotated
   benchmark table.
2. Deadline-aware loop: a wall-clock budget breaks the surface loop early and
   the JSONL run entry is ALWAYS written (partial results recorded, not lost).
3. Eval-only no-cloud-fallback: an empty local-model output during eval must
   NOT shell out to the Claude CLI; production drafting still can.

Conventions follow tests/test_autoresearch_robustness.py and
tests/test_eval_determinism_b166.py (stub the pipeline up to the model seam,
patch the low-level claude seam, use temp dirs).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import app.autoresearch.optimizer as opt
import app.generation.service as svc
from app.autoresearch.mutator import ConfigSurface
from app.evaluation.service import (
    EvalRequest,
    load_benchmark_cases,
    run_eval_suite,
    seed_benchmark_cases_from_golden,
)

# The real repo configs/ dir — get_mutable_surfaces reads the actual config
# YAMLs, so the loop needs them. We only swap the DB to a temp one.
_REPO_CONFIGS = Path(__file__).resolve().parents[1] / "configs"
_SCHEMA = Path(__file__).resolve().parents[1] / "docs" / "schema.sql"


def _isolated_configs(tmp_path: Path) -> Path:
    """Copy the repo configs/ into tmp so the loop's apply_mutation/revert writes
    there, never mutating the real repo configs (which would also race under
    xdist). The JSONL run log goes to ``<parent>/var`` = ``tmp_path/var``."""
    import shutil

    dst = tmp_path / "configs"
    shutil.copytree(_REPO_CONFIGS, dst)
    return dst


def _seed_db(db: Path) -> None:
    """Create the full schema (eval_runs etc.) + seed golden cases so a suite
    run with persist=True works against a temp DB."""
    from app.db.bootstrap import connect

    conn = connect(db)
    try:
        conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
        seed_benchmark_cases_from_golden(conn)
        conn.commit()
    finally:
        conn.close()


def _fake_surfaces() -> list[ConfigSurface]:
    """Two no-op surfaces so the loop has something to iterate without touching
    real config files. apply_mutation / revert_mutation are stubbed in the tests
    that use these, so only the field shape matters."""
    return [
        ConfigSurface(
            name="surf_a", config_file="retrieval/defaults.yaml", yaml_key="a",
            current_value=1, mutation_type="step", step_size=1, min_val=0, max_val=10,
        ),
        ConfigSurface(
            name="surf_b", config_file="retrieval/defaults.yaml", yaml_key="b",
            current_value=1, mutation_type="step", step_size=1, min_val=0, max_val=10,
        ),
    ]


# ── (a) restricted (golden) eval set ──────────────────────────────────


def _seed_mixed_db(db: Path) -> tuple[int, int]:
    """Seed a DB with the golden cases plus a batch of rotated (non-golden)
    cases, mimicking the live instance whose table is rotated weekly. Returns
    (golden_count, rotated_count)."""
    from app.db.bootstrap import connect

    conn = connect(db)
    try:
        conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
        golden = seed_benchmark_cases_from_golden(conn)
        rotated = 0
        for i in range(golden + 5):  # strictly more than golden, all non-golden
            conn.execute(
                """INSERT OR IGNORE INTO benchmark_cases
                   (case_key, category, prompt_text, expected_properties_json)
                   VALUES (?, 'work', 'rotated prompt', '{}')""",
                (f"rotated-case-{i}",),
            )
            rotated += 1
        conn.commit()
        return golden, rotated
    finally:
        conn.close()


def test_load_benchmark_cases_golden_prefix_filters(tmp_path):
    db = tmp_path / "mixed.db"
    golden, rotated = _seed_mixed_db(db)
    assert rotated > golden  # the full table is larger than the golden subset

    conn = sqlite3.connect(db)
    try:
        scoped = load_benchmark_cases(conn, case_prefix="golden-")
        full = load_benchmark_cases(conn)
    finally:
        conn.close()

    assert len(scoped) == golden
    assert all(c["case_key"].startswith("golden-") for c in scoped)
    # The scope is a strict, smaller subset of the full (rotated) table.
    assert len(full) > len(scoped)


def test_golden_prefix_reseeds_when_absent(tmp_path):
    """A rotated instance may have a non-empty table with NO golden cases. The
    prefix scope must re-seed golden.yaml so autoresearch always has its stable
    subset rather than silently scoring zero cases."""
    db = tmp_path / "rotated_only.db"
    conn = sqlite3.connect(db)
    try:
        seed_benchmark_cases_from_golden(conn)
        # Rename every golden case so none match the prefix anymore.
        conn.execute("UPDATE benchmark_cases SET case_key = replace(case_key, 'golden-', 'rot-')")
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM benchmark_cases WHERE case_key LIKE 'golden-%'"
        ).fetchone()[0] == 0

        scoped = load_benchmark_cases(conn, case_prefix="golden-")
    finally:
        conn.close()

    assert len(scoped) >= 1
    assert all(c["case_key"].startswith("golden-") for c in scoped)


def test_autoresearch_evaluates_only_golden_subset(tmp_path, monkeypatch):
    """run_autoresearch must pass the golden prefix to every eval suite, so it
    scores the small stable subset, not the whole rotated table."""
    db = tmp_path / "ar.db"
    golden, rotated = _seed_mixed_db(db)

    seen_prefixes: list[str | None] = []
    seen_counts: list[int] = []
    real_run_eval = opt.run_eval_suite

    def spy_run_eval(request, **kw):
        seen_prefixes.append(request.case_prefix)
        result = real_run_eval(request, **kw)
        seen_counts.append(result.total_cases)
        return result

    monkeypatch.setattr(opt, "run_eval_suite", spy_run_eval)

    # Trivial deterministic generation — every case "passes" cheaply.
    def gen(prompt, *, database_url, configs_dir):
        return {"draft": "A perfectly fine reply, long enough to score.",
                "detected_mode": "work", "confidence": "high", "precedent_count": 1}

    report = opt.run_autoresearch(
        configs_dir=_isolated_configs(tmp_path),
        database_url=f"sqlite:///{db}",
        generate_fn=gen,
        max_iterations=3,
    )

    assert seen_prefixes, "no eval suites ran"
    # Every suite (baseline + candidates) used the golden scope.
    assert all(p == "golden-" for p in seen_prefixes), seen_prefixes
    # And actually scored the golden subset, never the full rotated table.
    assert all(c == golden for c in seen_counts), seen_counts
    assert report.cases_per_eval == golden


# ── (b) deadline-aware loop always writes a JSONL entry ────────────────


def test_loop_respects_deadline_and_writes_partial_entry(tmp_path, monkeypatch):
    """With max_seconds tiny and time advancing on each eval, the surface loop
    must break early AND still write the JSONL run entry (partial, not lost)."""
    # _write_jsonl_entry writes to configs_dir.parent / "var", so give the loop
    # a configs/ subdir and assert on tmp_path/var.
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    db = tmp_path / "deadline.db"
    _seed_db(db)
    monkeypatch.setattr(opt, "get_mutable_surfaces", lambda *a, **kw: _fake_surfaces())

    # Simulate wall-clock advancing 100s per monotonic() read so the deadline
    # (1s) trips immediately after the baseline — no real sleeping.
    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 100.0
        return clock["t"]

    monkeypatch.setattr(opt.time, "monotonic", fake_monotonic)

    def gen(prompt, *, database_url, configs_dir):
        return {"draft": "A perfectly fine reply, long enough to score.",
                "detected_mode": "work", "confidence": "high", "precedent_count": 1}

    report = opt.run_autoresearch(
        configs_dir=configs_dir,
        database_url=f"sqlite:///{db}",
        generate_fn=gen,
        max_iterations=99,
        max_seconds=1.0,
    )

    assert report.deadline_hit is True
    # Baseline ran, but the deadline cut the surface loop before any candidate.
    assert report.total_eval_runs == 1

    # The JSONL entry was written despite the early break.
    jsonl = tmp_path / "var" / "autoresearch_runs.jsonl"
    assert jsonl.exists()
    entry = json.loads(jsonl.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["deadline_hit"] is True
    assert entry["iterations"] == 1
    assert entry["cases_per_eval"] >= 1


def test_jsonl_written_even_when_loop_raises(tmp_path, monkeypatch):
    """If the surface loop raises unexpectedly, the run log must still be
    written (results recorded, not silently lost)."""
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    db = tmp_path / "boom.db"
    _seed_db(db)
    monkeypatch.setattr(opt, "get_mutable_surfaces", lambda *a, **kw: _fake_surfaces())

    def gen(prompt, *, database_url, configs_dir):
        return {"draft": "A perfectly fine reply, long enough to score.",
                "detected_mode": "work", "confidence": "high", "precedent_count": 1}

    def explode(**kw):
        raise RuntimeError("surface loop blew up")

    monkeypatch.setattr(opt, "_run_surface_loop", explode)

    with pytest.raises(RuntimeError, match="surface loop blew up"):
        opt.run_autoresearch(
            configs_dir=configs_dir,
            database_url=f"sqlite:///{db}",
            generate_fn=gen,
            max_iterations=3,
        )

    jsonl = tmp_path / "var" / "autoresearch_runs.jsonl"
    assert jsonl.exists(), "run log must be written even when the loop raises"


# ── (c)+(d) eval-only no-cloud-fallback ───────────────────────────────


def _stub_generation_pipeline(monkeypatch):
    """Stub generate_draft's retrieval/persona/persistence path up to the model
    dispatch (mirrors tests/test_eval_determinism_b166.py)."""
    def _stub_retrieve(*a, **kw):
        return svc.RetrievalResponse(
            query="", retrieval_method="x", semantic_search_enabled=False,
            applied_filters={}, detected_mode=None, documents=[], chunks=[], reply_pairs=[],
        )

    monkeypatch.setattr(svc, "retrieve_context", _stub_retrieve)
    monkeypatch.setattr(svc, "_load_prompts", lambda _d: {"system_prompt": "S"})
    monkeypatch.setattr(svc, "_load_persona", lambda _d: {"style": {"avg_reply_words": 30}, "modes": {}})
    monkeypatch.setattr(svc, "lookup_sender_profile", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "lookup_facts", lambda **kw: [])
    monkeypatch.setattr(svc, "_lookup_prior_reply_to_sender", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "_local_model_available", lambda: True)
    monkeypatch.setattr(svc, "_adapter_available", lambda: False)
    monkeypatch.setattr(svc, "_persona_routing_enabled", lambda: False)
    monkeypatch.setattr(svc, "generate_subject", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "_log_draft_event", lambda *a, **kw: False)
    monkeypatch.setattr(svc, "_connect", lambda _p: sqlite3.connect(":memory:"))
    monkeypatch.setattr(svc, "resolve_sqlite_path", lambda _u: Path("/tmp/x.db"))
    monkeypatch.setattr(svc, "_resolve_decoding", lambda intent, conf: (None, None))
    monkeypatch.setattr(svc, "_multi_candidate_config", lambda: {"enabled": False, "temperatures": []})
    # The global fallback config says "claude" — only the per-request eval flag
    # should suppress it.
    monkeypatch.setattr(svc, "get_model_fallback", lambda: "claude")


def _claude_tripwire(monkeypatch):
    """Patch the Claude CLI seam to fail loudly if invoked."""
    def boom(*a, **kw):
        raise AssertionError("_call_claude_cli must NOT be invoked")

    monkeypatch.setattr(svc, "_call_claude_cli", boom)


def _empty_local(monkeypatch):
    """Make the local model return an empty draft on every call."""
    monkeypatch.setattr(svc, "_call_local_model",
                        lambda *a, **kw: "")


def _req(**kw):
    return svc.DraftRequest(inbound_message="Can we meet next week?", **kw)


def _generate(req):
    return svc.generate_draft(req, database_url="sqlite:///x", configs_dir=Path("/tmp"))


def test_eval_empty_output_does_not_call_claude(monkeypatch):
    """An eval request (no_cloud_fallback=True) with empty local output must NOT
    shell to the Claude CLI; it raises a soft ValueError instead, which the eval
    suite catches per-case."""
    _stub_generation_pipeline(monkeypatch)
    _claude_tripwire(monkeypatch)
    _empty_local(monkeypatch)

    with pytest.raises(ValueError, match="empty output"):
        _generate(_req(deterministic=True, seed=svc.EVAL_SEED, no_cloud_fallback=True))
    # If _call_claude_cli had been reached, the tripwire would have raised
    # AssertionError instead of the expected ValueError.


def test_eval_empty_output_handled_soft_in_suite(tmp_path, monkeypatch):
    """End-to-end: an eval generate_fn that yields empty/raising output must be
    absorbed by run_eval_suite (case scored, suite continues) — NOT bubble a
    Claude-CLI RuntimeError. We patch the Claude seam as a tripwire and route
    generation through the real generate_draft with the eval flag set."""
    _stub_generation_pipeline(monkeypatch)
    _claude_tripwire(monkeypatch)
    _empty_local(monkeypatch)

    db = tmp_path / "eval.db"
    seed_benchmark_cases_from_golden(sqlite3.connect(db))

    def gen(prompt, *, database_url, configs_dir):
        resp = _generate(_req(deterministic=True, seed=svc.EVAL_SEED, no_cloud_fallback=True))
        return {"draft": resp.draft, "detected_mode": resp.detected_mode,
                "confidence": resp.confidence, "precedent_count": len(resp.precedent_used)}

    result = run_eval_suite(
        EvalRequest(config_tag="eval-empty", case_prefix="golden-"),
        generate_fn=gen,
        database_url=f"sqlite:///{db}",
        configs_dir=tmp_path,
        persist=False,
    )
    # Every golden case ran and scored (as a fail, empty draft) — none aborted.
    assert result.total_cases >= 1
    assert result.failed == result.total_cases


def test_production_drafting_still_uses_claude_fallback(monkeypatch):
    """Production drafting (no_cloud_fallback left False, strict_local False)
    must STILL fall back to Claude on empty local output — the suppression is
    eval-only."""
    _stub_generation_pipeline(monkeypatch)
    _empty_local(monkeypatch)

    called = {"claude": 0}

    def fake_claude(prompt, *, max_tokens=300):
        called["claude"] += 1
        return "A Claude-generated fallback reply, long enough to score."

    monkeypatch.setattr(svc, "_call_claude_cli", fake_claude)

    resp = _generate(_req())  # plain production request
    assert called["claude"] >= 1, "production drafting must still use the claude fallback"
    assert "Claude-generated fallback" in resp.draft
