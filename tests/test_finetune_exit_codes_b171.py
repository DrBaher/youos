"""Exit-code / failure-surfacing tests for scripts/finetune_lora.py (b171).

Before b171, ``run_training`` returned ``None`` on every terminal path and
``main`` never propagated, so the process always exited 0. The nightly pipeline's
``_run_step`` then recorded ``finetune: true`` / ``[OK]`` even when mlx training
crashed or the run produced no adapter — a broken/never-ran fine-tune was
indistinguishable from a real one.

These tests pin the failure-vs-skip boundary in run_training/main:
  * no/too-little fresh data, or --dry-run  -> SKIP    -> exit 0 (quiet night)
  * mlx nonzero return                       -> FAILURE -> exit 1
  * a run that produced no/corrupt adapter   -> FAILURE -> exit 1
  * a real train that produced+promoted one  -> SUCCESS -> exit 0

The nightly side (step_finetune_lora -> _run_step) already maps a nonzero child
exit to ``False`` ([WARN]) and a zero exit to ``True`` ([OK]); that mapping is
asserted here too so the end-to-end honesty is covered (a genuine failure now
exits nonzero, which _run_step reports as finetune: false).

Hermetic: no real mlx, no real model, no network. The training subprocess is
mocked and everything lives under tmp_path.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    path = REPO / rel
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ft():
    return _load("finetune_lora_b171", "scripts/finetune_lora.py")


def _args(data_dir: Path, adapter_dir: Path, **over) -> argparse.Namespace:
    base = dict(
        data_dir=str(data_dir),
        adapter_dir=str(adapter_dir),
        db=str(data_dir / "missing.db"),  # absent -> no DB write
        iters=None,
        num_layers=None,
        learning_rate=None,
        auto=True,
        dry_run=False,
        dpo=False,
        persona=None,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _write_train(data_dir: Path, n: int) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    lines = ['{"text": "example %d"}' % i for i in range(n)]
    (data_dir / "train.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


class _FakeProc:
    def __init__(self, returncode: int, stderr: str = "", stdout: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# --- (a) missing / too-little data -> SKIP (exit 0, NOT failure) -------------


def test_missing_train_jsonl_is_skip(ft, tmp_path):
    res = ft.run_training(_args(tmp_path / "data", tmp_path / "adapter"))
    assert res == ft.RESULT_SKIP


def test_too_few_examples_is_skip(ft, tmp_path):
    data = tmp_path / "data"
    _write_train(data, 2)  # < 3
    res = ft.run_training(_args(data, tmp_path / "adapter"))
    assert res == ft.RESULT_SKIP


def test_dry_run_is_skip(ft, tmp_path):
    data = tmp_path / "data"
    _write_train(data, 5)
    res = ft.run_training(_args(data, tmp_path / "adapter", dry_run=True))
    assert res == ft.RESULT_SKIP


def test_skip_main_exits_zero(ft, tmp_path, monkeypatch):
    """main() over a skip path must not raise SystemExit(nonzero)."""
    data = tmp_path / "data"  # no train.jsonl -> SKIP
    monkeypatch.setattr(ft, "parse_args", lambda: _args(data, tmp_path / "adapter"))
    ft.main()  # returns None, no exception


# --- (b) mlx nonzero return -> FAILURE (exit 1) -----------------------------


def test_mlx_nonzero_is_failure(ft, tmp_path, monkeypatch):
    data = tmp_path / "data"
    _write_train(data, 5)
    monkeypatch.setattr(
        ft.subprocess, "run", lambda *a, **k: _FakeProc(1, "boom traceback")
    )
    res = ft.run_training(_args(data, tmp_path / "adapter"))
    assert res == ft.RESULT_FAILURE


def test_mlx_nonzero_main_exits_one(ft, tmp_path, monkeypatch):
    data = tmp_path / "data"
    _write_train(data, 5)
    monkeypatch.setattr(ft.subprocess, "run", lambda *a, **k: _FakeProc(2, "boom"))
    monkeypatch.setattr(ft, "parse_args", lambda: _args(data, tmp_path / "adapter"))
    with pytest.raises(SystemExit) as exc:
        ft.main()
    assert exc.value.code == 1


# --- (c) run produced no/corrupt adapter -> FAILURE -------------------------


def test_no_adapter_after_run_is_failure(ft, tmp_path, monkeypatch):
    data = tmp_path / "data"
    _write_train(data, 5)
    # mlx "succeeds" but writes no adapter -> _promote_adapter returns False.
    monkeypatch.setattr(ft.subprocess, "run", lambda *a, **k: _FakeProc(0))
    res = ft.run_training(_args(data, tmp_path / "adapter"))
    assert res == ft.RESULT_FAILURE


def test_promotion_failure_is_failure(ft, tmp_path, monkeypatch):
    data = tmp_path / "data"
    _write_train(data, 5)
    monkeypatch.setattr(ft.subprocess, "run", lambda *a, **k: _FakeProc(0))
    # Even if mlx "wrote" something, a failed promotion is a hard failure.
    monkeypatch.setattr(ft, "_promote_adapter", lambda *a, **k: False)
    res = ft.run_training(_args(data, tmp_path / "adapter"))
    assert res == ft.RESULT_FAILURE


# --- (d) successful train -> SUCCESS (exit 0) -------------------------------


def test_successful_train_is_success(ft, tmp_path, monkeypatch):
    data = tmp_path / "data"
    adapter = tmp_path / "adapter"
    _write_train(data, 5)
    monkeypatch.setattr(ft.subprocess, "run", lambda *a, **k: _FakeProc(0))
    # Stub promotion to succeed without a real safetensors file.
    monkeypatch.setattr(ft, "_promote_adapter", lambda *a, **k: True)
    res = ft.run_training(_args(data, adapter))
    assert res == ft.RESULT_SUCCESS
    # Metadata is written on success.
    assert (adapter / "meta.json").exists()


def test_successful_train_main_exits_zero(ft, tmp_path, monkeypatch):
    data = tmp_path / "data"
    _write_train(data, 5)
    monkeypatch.setattr(ft.subprocess, "run", lambda *a, **k: _FakeProc(0))
    monkeypatch.setattr(ft, "_promote_adapter", lambda *a, **k: True)
    monkeypatch.setattr(ft, "parse_args", lambda: _args(data, tmp_path / "adapter"))
    ft.main()  # no SystemExit


# --- (e) nightly _run_step maps the child exit code to OK/WARN --------------
#
# step_finetune_lora(verbose=False) -> bool delegates to _run_step, which
# returns False ([WARN]) when the child process exits nonzero and True ([OK])
# when it exits 0. With the b171 fix, a genuine training failure now exits
# nonzero, so the nightly correctly records finetune: false instead of true.


@pytest.fixture
def nightly():
    return _load("nightly_pipeline_b171", "scripts/nightly_pipeline.py")


def test_step_finetune_lora_maps_nonzero_to_false(nightly, monkeypatch):
    monkeypatch.setattr(
        nightly.subprocess,
        "run",
        lambda *a, **k: _FakeProc(1, "mlx crashed", "starting"),
    )
    assert nightly.step_finetune_lora(verbose=False) is False


def test_step_finetune_lora_maps_zero_to_true(nightly, monkeypatch):
    monkeypatch.setattr(
        nightly.subprocess, "run", lambda *a, **k: _FakeProc(0, "", "done")
    )
    assert nightly.step_finetune_lora(verbose=False) is True


def test_run_step_nonzero_is_warn_false(nightly, monkeypatch):
    """The exit-code -> bool contract _run_step exposes to the pipeline."""
    monkeypatch.setattr(
        nightly.subprocess, "run", lambda *a, **k: _FakeProc(1, "err", "out")
    )
    assert nightly._run_step("x", ["true"]) is False
    monkeypatch.setattr(
        nightly.subprocess, "run", lambda *a, **k: _FakeProc(0, "", "out")
    )
    assert nightly._run_step("x", ["true"]) is True
