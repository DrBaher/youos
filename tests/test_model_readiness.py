"""Model readiness gate — ask users to wait until LoRA is trained AND benchmarked.

Pins the phase machine (not_started → training → benchmarking → benchmark_pending
→ ready), the wizard chaining the golden eval, and the soft banner wiring.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from app.core.stats import get_model_readiness


def _make_adapter(tmp_path: Path) -> Path:
    adir = tmp_path / "adapters" / "latest"
    adir.mkdir(parents=True)
    (adir / "adapters.safetensors").write_text("fake")
    return adir


def test_not_started_when_no_adapter(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.stats._resolve_adapter_path", lambda: tmp_path / "none")
    s = get_model_readiness("sqlite:///x", finetune_running=False)
    assert s["phase"] == "not_started" and s["ready"] is False


def test_training_when_running_and_no_adapter(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.stats._resolve_adapter_path", lambda: tmp_path / "none")
    s = get_model_readiness("sqlite:///x", finetune_running=True)
    assert s["phase"] == "training" and s["ready"] is False


def test_benchmarking_when_running_with_adapter(tmp_path, monkeypatch):
    adir = _make_adapter(tmp_path)
    monkeypatch.setattr("app.core.stats._resolve_adapter_path", lambda: adir)
    s = get_model_readiness("sqlite:///x", finetune_running=True)
    assert s["phase"] == "benchmarking" and s["ready"] is False


def test_benchmark_pending_when_trained_but_not_benchmarked(tmp_path, monkeypatch):
    adir = _make_adapter(tmp_path)
    monkeypatch.setattr("app.core.stats._resolve_adapter_path", lambda: adir)
    # No golden_results.json anywhere → not benchmarked.
    monkeypatch.setattr("app.core.stats._get_var_path", lambda name: tmp_path / "var" / name)
    s = get_model_readiness("sqlite:///x", finetune_running=False)
    assert s["phase"] == "benchmark_pending" and s["ready"] is False


def test_ready_when_benchmark_newer_than_adapter(tmp_path, monkeypatch):
    adir = _make_adapter(tmp_path)
    monkeypatch.setattr("app.core.stats._resolve_adapter_path", lambda: adir)
    var = tmp_path / "var"
    var.mkdir()
    golden = var / "golden_results.json"
    golden.write_text("{}")
    # Ensure the benchmark is newer than the adapter.
    future = time.time() + 100
    os.utime(golden, (future, future))
    monkeypatch.setattr("app.core.stats._get_var_path", lambda name: var / name)
    s = get_model_readiness("sqlite:///x", finetune_running=False)
    assert s["phase"] == "ready" and s["ready"] is True


def test_stale_benchmark_is_not_ready(tmp_path, monkeypatch):
    adir = _make_adapter(tmp_path)
    monkeypatch.setattr("app.core.stats._resolve_adapter_path", lambda: adir)
    var = tmp_path / "var"
    var.mkdir()
    golden = var / "golden_results.json"
    golden.write_text("{}")
    # Benchmark OLDER than the adapter → trained since last benchmark → pending.
    past = time.time() - 1000
    os.utime(golden, (past, past))
    monkeypatch.setattr("app.core.stats._get_var_path", lambda name: var / name)
    s = get_model_readiness("sqlite:///x", finetune_running=False)
    assert s["phase"] == "benchmark_pending"


# --- wiring -----------------------------------------------------------------


def test_wizard_finetune_chains_golden_eval():
    src = (Path(__file__).resolve().parents[1] / "app" / "api" / "stats_routes.py").read_text()
    assert "run_golden_eval.py" in src  # benchmark chained after finetune
    assert "/api/model/readiness" in src  # readiness endpoint exists


def test_drafting_page_has_wait_banner():
    content = (Path(__file__).resolve().parents[1] / "templates" / "feedback.html").read_text()
    assert 'id="modelReadyBanner"' in content
    assert "/api/model/readiness" in content
    assert "Draft anyway" in content  # soft gate — can proceed
    # Refresh gives visible progress, and the gate is actionable: run the benchmark.
    assert "Checking…" in content
    assert 'id="mrbBenchmark"' in content and "/api/benchmark" in content
