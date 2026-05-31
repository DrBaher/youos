"""Tests for the golden-eval gate hardening (b170).

Three eval-side correctness fixes (production draft GENERATION is unchanged):
  1. Honest reporting in nightly_pipeline.step_golden_eval: print [OK] only
     when the composite clears GOLDEN_PASS_BAR, [WARN] otherwise, so the log
     matches the returned (and JSON-recorded) pass/fail.
  2. Deterministic golden-eval generation: the golden-eval entry points build
     a DraftRequest with deterministic=True, seed=EVAL_SEED,
     no_cloud_fallback=True (the determinism deferred from b166, where only
     autoresearch's _generate_for_eval got it).
  3. Promotion gate serves the BEST-AVAILABLE adapter (don't-regress), NOT the
     aspirational quality target: a WARM-path candidate that holds vs baseline
     (within tolerance) is PROMOTED even when sub-target (e.g. ~0.30), so new
     feedback keeps reaching the served model. A regressing candidate is rolled
     back; cold start is promoted unconditionally; a degenerate eval refuses.
     The step's pass/fail vs the 0.5 TARGET and the promotion decision answer
     different questions and may legitimately differ.

Hermetic: no real model, no network, temp paths. We capture the DraftRequest
handed to generate_draft and the stdout of the nightly step.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import scripts.nightly_pipeline as np
import scripts.run_eval as run_eval
import scripts.run_golden_eval as rge
from app.evaluation.promotion import (
    DEFAULT_MIN_FLOOR,
    gate_after_eval,
    should_promote,
)
from app.generation.service import EVAL_SEED

# Live baheros golden composites sit around 0.30 — well below the 0.5 quality
# TARGET. These sub-target-but-non-regressing scores must be PROMOTED, never
# rolled back, or the self-improvement loop freezes.
SUB_TARGET = 0.30


# ---------------------------------------------------------------------------
# 1. Honest reporting in step_golden_eval
# ---------------------------------------------------------------------------
def _run_step(monkeypatch, tmp_path, composite, capsys):
    """Drive step_golden_eval with a stubbed golden suite at a chosen composite.

    composite = passed/total, so we feed total cases with `passed` passes.
    """
    db = tmp_path / "youos.db"
    db.write_text("x")  # exists -> skip-on-no-DB gate passes
    monkeypatch.setattr(np, "DEFAULT_DB", db)
    monkeypatch.setattr(np, "_count_feedback_pairs", lambda _p: 50)

    total = 10
    passed = round(composite * total)
    summary = {
        "total": total,
        "passed": passed,
        "warned": 0,
        "failed": total - passed,
        "empty_count": 0,
        "empty_rate": 0.0,
        "degenerate": False,
        "results": [],
    }
    # step_golden_eval imports these locally from scripts.run_golden_eval.
    monkeypatch.setattr(rge, "run_golden_eval", lambda **kw: summary)
    monkeypatch.setattr(rge, "save_results", lambda *a, **kw: None)
    # ...and builds a real DraftRequest + calls generate_draft; stub the model.
    monkeypatch.setattr(
        "app.generation.service.generate_draft",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should be stubbed via run_golden_eval")),
    )

    result = np.step_golden_eval()
    out = capsys.readouterr().out
    return result, out


def test_step_reports_ok_only_when_passing(monkeypatch, tmp_path, capsys):
    result, out = _run_step(monkeypatch, tmp_path, 0.7, capsys)
    assert result is True
    assert "[OK]" in out
    assert "[WARN]" not in out


def test_step_reports_warn_when_below_bar(monkeypatch, tmp_path, capsys):
    result, out = _run_step(monkeypatch, tmp_path, 0.3, capsys)
    assert result is False
    assert "[WARN]" in out
    assert "below target" in out
    # The misleading unconditional "[OK] Golden evaluation completed" is gone.
    assert "Golden evaluation completed" not in out


# ---------------------------------------------------------------------------
# 2. Deterministic golden-eval generation
# ---------------------------------------------------------------------------
def _capture_request_from_step(monkeypatch, tmp_path):
    """Run step_golden_eval's internal _generate once and return the
    DraftRequest it handed to generate_draft."""
    db = tmp_path / "youos.db"
    db.write_text("x")
    monkeypatch.setattr(np, "DEFAULT_DB", db)
    monkeypatch.setattr(np, "_count_feedback_pairs", lambda _p: 50)
    monkeypatch.setattr(rge, "save_results", lambda *a, **kw: None)

    captured = {}

    def fake_run_golden_eval(*, generate_fn, database_url=None, configs_dir=None):
        # Invoke the closure exactly as the real suite would, capturing the req.
        generate_fn("hello", database_url=database_url, configs_dir=configs_dir)
        return {"total": 1, "passed": 1, "empty_rate": 0.0, "degenerate": False}

    monkeypatch.setattr(rge, "run_golden_eval", fake_run_golden_eval)

    def fake_generate(req, *, database_url=None, configs_dir=None):
        captured["req"] = req
        return mock.MagicMock(
            draft="a real draft", detected_mode="work", detected_language=None
        )

    monkeypatch.setattr("app.generation.service.generate_draft", fake_generate)
    np.step_golden_eval()
    return captured["req"]


def test_nightly_golden_request_is_deterministic(monkeypatch, tmp_path):
    req = _capture_request_from_step(monkeypatch, tmp_path)
    assert req.deterministic is True
    assert req.seed == EVAL_SEED
    assert req.no_cloud_fallback is True


def test_run_golden_eval_main_request_is_deterministic(monkeypatch):
    """scripts/run_golden_eval.py::main's _generate closure is deterministic."""
    captured = {}

    def fake_generate(req, *, database_url=None, configs_dir=None):
        captured["req"] = req
        return mock.MagicMock(draft="x", detected_mode="work", confidence=0.5)

    def fake_run_golden_eval(*, generate_fn, database_url=None, configs_dir=None, golden_path=None):
        generate_fn("hi", database_url=database_url, configs_dir=configs_dir)
        return {"total": 1, "passed": 1, "results": []}

    monkeypatch.setattr("app.generation.service.generate_draft", fake_generate)
    monkeypatch.setattr(rge, "run_golden_eval", fake_run_golden_eval)
    monkeypatch.setattr(rge, "save_results", lambda *a, **kw: None)
    monkeypatch.setattr(rge, "format_scorecard", lambda s: "")
    monkeypatch.setattr(
        "app.core.settings.get_settings",
        lambda: mock.MagicMock(configs_dir=Path("/tmp"), database_url="sqlite:///x"),
    )
    monkeypatch.setattr(
        "app.db.bootstrap.resolve_sqlite_path", lambda _u: Path("/tmp/x.db")
    )
    monkeypatch.setattr("sys.argv", ["run_golden_eval", "--summary-only"])
    rge.main()
    req = captured["req"]
    assert req.deterministic is True
    assert req.seed == EVAL_SEED
    assert req.no_cloud_fallback is True


def test_run_eval_request_is_deterministic(monkeypatch):
    """scripts/run_eval.py::_generate_for_eval is deterministic too."""
    captured = {}

    def fake_generate(req, *, database_url=None, configs_dir=None):
        captured["req"] = req
        return mock.MagicMock(
            draft="x", detected_mode="work", confidence=0.5, precedent_used=[]
        )

    # run_eval imports generate_draft by name at module load, so patch the
    # binding on the run_eval module itself, not the source module.
    monkeypatch.setattr(run_eval, "generate_draft", fake_generate)
    run_eval._generate_for_eval(
        "hi", database_url="sqlite:///x", configs_dir=Path("/tmp")
    )
    req = captured["req"]
    assert req.deterministic is True
    assert req.seed == EVAL_SEED
    assert req.no_cloud_fallback is True


# ---------------------------------------------------------------------------
# 3. Promotion gate = serve best-available (don't-regress), NOT the 0.5 target
# ---------------------------------------------------------------------------
def test_cold_start_promotes_unconditionally():
    """No baseline (cold start / first finetune) => promote, so drafting is
    never left without an adapter."""
    ok, reason = should_promote(0.12, None)
    assert ok is True
    assert "cold start" in reason


def test_warm_sub_target_non_regressing_is_promoted():
    """The corrected behavior: a sub-0.5 candidate that does NOT regress vs the
    prior adapter (0.30 vs baseline 0.28) is PROMOTED — not rolled back — so the
    self-improvement loop keeps moving at current (~0.30) quality."""
    ok, reason = should_promote(SUB_TARGET, 0.28)
    assert ok is True
    assert "kept" in reason
    # The reason is honest that it's below the aspirational target but kept.
    assert "below target" in reason


def test_warm_regression_beyond_tolerance_is_rolled_back():
    """A candidate that regresses past tolerance (0.20 vs baseline 0.40) is
    rolled back regardless of the absolute level."""
    ok, reason = should_promote(0.20, 0.40)
    assert ok is False
    assert "regressed" in reason


def test_warm_above_target_and_holding_promotes():
    ok, _ = should_promote(0.55, 0.50)
    assert ok is True


def test_degenerate_still_hard_refuse_even_cold_start():
    ok, reason = should_promote(None, None, eval_degenerate=True)
    assert ok is False
    assert "degenerate" in reason


def test_default_floor_is_disabled_not_target():
    """The min_floor default must NOT equal the quality target — it is a
    disabled (0.0) safety floor so the warm path is a pure non-regression gate.
    A 0.5 floor would roll back every ~0.30 retrain and freeze updates."""
    assert DEFAULT_MIN_FLOOR == 0.0
    assert DEFAULT_MIN_FLOOR != np.GOLDEN_PASS_BAR


def _write_adapter(d, content):
    d.mkdir(parents=True, exist_ok=True)
    (d / "adapters.safetensors").write_text(content, encoding="utf-8")
    (d / "meta.json").write_text("{}", encoding="utf-8")


def test_gate_keeps_sub_target_non_regressing_warm(tmp_path):
    """End-to-end through gate_after_eval: a warm sub-target candidate that
    holds vs baseline is KEPT (new adapter stays live, not rolled back)."""
    from app.evaluation.promotion import snapshot_adapter

    latest = tmp_path / "latest"
    prev = tmp_path / "previous"
    _write_adapter(latest, "GOOD")
    snapshot_adapter(latest, prev)
    (latest / "adapters.safetensors").write_text("NEWER", encoding="utf-8")

    res = gate_after_eval(
        candidate_composite=SUB_TARGET,
        baseline_composite=0.28,  # sub-target but doesn't regress
        latest_dir=latest,
        previous_dir=prev,
    )
    assert res["action"] == "kept"
    assert (latest / "adapters.safetensors").read_text() == "NEWER"


def test_gate_rolls_back_regressing_warm(tmp_path):
    """A regressing warm candidate restores the pre-finetune snapshot."""
    from app.evaluation.promotion import snapshot_adapter

    latest = tmp_path / "latest"
    prev = tmp_path / "previous"
    _write_adapter(latest, "GOOD")
    snapshot_adapter(latest, prev)
    (latest / "adapters.safetensors").write_text("BAD", encoding="utf-8")

    res = gate_after_eval(
        candidate_composite=0.20,
        baseline_composite=0.40,  # regresses beyond tolerance
        latest_dir=latest,
        previous_dir=prev,
    )
    assert res["action"] == "rolled_back"
    assert (latest / "adapters.safetensors").read_text() == "GOOD"


def test_gate_keeps_cold_start_sub_target(tmp_path):
    """Cold start (no baseline) keeps the new adapter even sub-target."""
    latest = tmp_path / "latest"
    prev = tmp_path / "previous"
    _write_adapter(latest, "FIRST")
    res = gate_after_eval(
        candidate_composite=0.20,
        baseline_composite=None,
        latest_dir=latest,
        previous_dir=prev,
    )
    assert res["action"] == "kept"
    assert (latest / "adapters.safetensors").read_text() == "FIRST"
