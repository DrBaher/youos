"""b176: memory-lean finetune so a 4B/8B base fits a 16 GB M4 budget.

The scheduled nightly retrain OOMed after the b174 migration: the wrapper
auto-scaled to a flat 16 LoRA layers and passed no grad-checkpoint and no
sequence-length cap on the new Qwen3-4B-4bit base, blowing past the ~12.7 GB
GPU working set. The manual retrain only fit with --grad-checkpoint +
--max-seq-length 1024 + --num-layers 8 (peak 3.97 GB). These tests assert
(hermetically -- no real mlx, no real train) that the wrapper now applies
those memory levers automatically based on the base-model size / RAM:

  - a 4B base gets --grad-checkpoint, a capped --max-seq-length, and a
    reduced --num-layers (<= 8);
  - a small 1.5B base stays close to the prior behavior (no grad-checkpoint,
    no num-layers cap, larger seq window);
  - an 8B base is tightened further (<= 4 layers);
  - config model.finetune.* overrides win;
  - the b171 SKIP path still exits 0 (no false alarm on quiet nights);
  - the chosen knobs land in meta.json (observability).
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
    return _load("finetune_lora_b176", "scripts/finetune_lora.py")


# --- model-size detection -------------------------------------------------


def test_parse_model_size_variants(ft):
    assert ft._parse_model_size_b("mlx-community/Qwen3-4B-Instruct-2507-4bit") == 4.0
    assert ft._parse_model_size_b("mlx-community/Qwen2.5-1.5B-Instruct-4bit") == 1.5
    assert ft._parse_model_size_b("meta/Llama-3.1-8B-Instruct") == 8.0
    # "4bit" must NOT be read as 4 billion params.
    assert ft._parse_model_size_b("some/model-4bit") is None
    assert ft._parse_model_size_b("some/model") is None


# --- memory config policy -------------------------------------------------


def test_4b_memory_config_is_lean(ft):
    mem = ft.compute_memory_config("Qwen3-4B-Instruct-4bit", sys_mem_gb=16.0)
    assert mem["grad_checkpoint"] is True
    assert mem["max_seq_length"] == 1024
    assert mem["num_layers_cap"] == 8
    assert mem["batch_size_cap"] == 1


def test_1p5b_memory_config_is_unconstrained(ft):
    mem = ft.compute_memory_config("Qwen2.5-1.5B-Instruct-4bit", sys_mem_gb=16.0)
    assert mem["grad_checkpoint"] is False
    assert mem["num_layers_cap"] is None
    assert mem["batch_size_cap"] is None
    assert mem["max_seq_length"] >= 1024


def test_8b_memory_config_is_tighter(ft):
    mem = ft.compute_memory_config("Llama-3.1-8B-Instruct-4bit", sys_mem_gb=16.0)
    assert mem["grad_checkpoint"] is True
    assert mem["num_layers_cap"] == 4


def test_unknown_size_assumes_constrained_tier(ft):
    # No size token -> assume the constrained default-base (4B) tier.
    mem = ft.compute_memory_config("some/mystery-model", sys_mem_gb=16.0)
    assert mem["grad_checkpoint"] is True
    assert mem["num_layers_cap"] == 8


def test_low_ram_forces_checkpoint_for_mid_base(ft):
    # On a tight-RAM box even a mid base should checkpoint + batch=1.
    mem = ft.compute_memory_config("Qwen3-4B-Instruct-4bit", sys_mem_gb=18.0)
    assert mem["grad_checkpoint"] is True
    assert mem["batch_size_cap"] == 1


# --- resolved knobs -------------------------------------------------------


def test_4b_knobs_apply_memory_caps(ft):
    auto = ft.compute_auto_config(300)  # data-size pick: 16 layers
    knobs = ft.resolve_train_knobs(
        "mlx-community/Qwen3-4B-Instruct-2507-4bit",
        auto,
        overrides={},
        sys_mem_gb=16.0,
    )
    assert knobs["grad_checkpoint"] is True
    assert knobs["max_seq_length"] <= 1024
    assert knobs["num_layers"] <= 8  # memory cap pulled 16 -> 8
    assert knobs["batch_size"] == 1


def test_1p5b_knobs_stay_near_prior(ft):
    auto = ft.compute_auto_config(300)
    knobs = ft.resolve_train_knobs(
        "mlx-community/Qwen2.5-1.5B-Instruct-4bit",
        auto,
        overrides={},
        sys_mem_gb=16.0,
    )
    assert knobs["grad_checkpoint"] is False
    assert knobs["num_layers"] == 16  # no cap: keeps data-size pick
    assert knobs["max_seq_length"] >= 1024


def test_8b_knobs_are_tightest(ft):
    auto = ft.compute_auto_config(300)
    knobs = ft.resolve_train_knobs(
        "meta/Llama-3.1-8B-Instruct-4bit", auto, overrides={}, sys_mem_gb=16.0
    )
    assert knobs["grad_checkpoint"] is True
    assert knobs["num_layers"] <= 4


def test_config_overrides_win(ft):
    auto = ft.compute_auto_config(300)
    overrides = {
        "grad_checkpoint": False,
        "max_seq_length": 512,
        "num_layers": 6,
        "batch_size": 3,
    }
    knobs = ft.resolve_train_knobs(
        "mlx-community/Qwen3-4B-Instruct-2507-4bit",
        auto,
        overrides=overrides,
        sys_mem_gb=16.0,
    )
    assert knobs["grad_checkpoint"] is False
    assert knobs["max_seq_length"] == 512
    assert knobs["num_layers"] == 6
    assert knobs["batch_size"] == 3


def test_cli_flag_beats_memory_cap(ft):
    # An explicit --num-layers must win even over the memory cap.
    auto = ft.compute_auto_config(300)
    knobs = ft.resolve_train_knobs(
        "mlx-community/Qwen3-4B-Instruct-2507-4bit",
        auto,
        num_layers_override=12,
        overrides={},
        sys_mem_gb=16.0,
    )
    assert knobs["num_layers"] == 12


def test_override_can_force_grad_checkpoint_on_small_base(ft):
    auto = ft.compute_auto_config(300)
    knobs = ft.resolve_train_knobs(
        "mlx-community/Qwen2.5-1.5B-Instruct-4bit",
        auto,
        overrides={"grad_checkpoint": True},
        sys_mem_gb=16.0,
    )
    assert knobs["grad_checkpoint"] is True


# --- the actually-built mlx_lm command ------------------------------------


class _FakeProc:
    def __init__(self, returncode=0, stderr="", stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _args(data_dir: Path, adapter_dir: Path, **over) -> argparse.Namespace:
    base = dict(
        data_dir=str(data_dir),
        adapter_dir=str(adapter_dir),
        db=str(data_dir / "missing.db"),
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


def _captured_cmd(ft, tmp_path, base_model, monkeypatch, n=120, overrides=None):
    data = tmp_path / "data"
    adapter = tmp_path / "adapter"
    _write_train(data, n)
    monkeypatch.setattr(ft, "BASE_MODEL", base_model)
    monkeypatch.setattr(ft, "_system_mem_gb", lambda: 16.0)
    monkeypatch.setattr(ft, "_finetune_overrides", lambda: dict(overrides or {}))
    monkeypatch.setattr(ft, "_promote_adapter", lambda *a, **k: True)
    captured = {}

    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        return _FakeProc(0)

    monkeypatch.setattr(ft.subprocess, "run", fake_run)
    res = ft.run_training(_args(data, adapter))
    assert res == ft.RESULT_SUCCESS
    return captured["cmd"], adapter


def _flag_value(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_built_command_4b_has_memory_levers(ft, tmp_path, monkeypatch):
    cmd, adapter = _captured_cmd(
        ft, tmp_path, "mlx-community/Qwen3-4B-Instruct-2507-4bit", monkeypatch
    )
    assert "--grad-checkpoint" in cmd
    assert "--max-seq-length" in cmd
    assert int(_flag_value(cmd, "--max-seq-length")) <= 1024
    assert int(_flag_value(cmd, "--num-layers")) <= 8
    assert int(_flag_value(cmd, "--batch-size")) == 1
    # meta.json records the chosen knobs for observability.
    import json

    meta = json.loads((adapter / "meta.json").read_text())
    assert meta["grad_checkpoint"] is True
    assert meta["max_seq_length"] <= 1024
    assert meta["num_layers"] <= 8


def test_built_command_1p5b_near_prior(ft, tmp_path, monkeypatch):
    cmd, _ = _captured_cmd(
        ft, tmp_path, "mlx-community/Qwen2.5-1.5B-Instruct-4bit", monkeypatch
    )
    assert "--grad-checkpoint" not in cmd
    assert int(_flag_value(cmd, "--num-layers")) == 16
    assert int(_flag_value(cmd, "--max-seq-length")) >= 1024


def test_built_command_respects_config_override(ft, tmp_path, monkeypatch):
    cmd, _ = _captured_cmd(
        ft,
        tmp_path,
        "mlx-community/Qwen3-4B-Instruct-2507-4bit",
        monkeypatch,
        overrides={"max_seq_length": 512, "num_layers": 6, "grad_checkpoint": False},
    )
    assert "--grad-checkpoint" not in cmd
    assert int(_flag_value(cmd, "--max-seq-length")) == 512
    assert int(_flag_value(cmd, "--num-layers")) == 6


# --- b171 invariants preserved --------------------------------------------


def test_skip_path_still_exits_zero(ft, tmp_path, monkeypatch):
    # No train.jsonl -> SKIP -> main() must not raise (exit 0).
    data = tmp_path / "data"
    monkeypatch.setattr(ft, "parse_args", lambda: _args(data, tmp_path / "adapter"))
    ft.main()  # no SystemExit


def test_too_few_pairs_is_skip(ft, tmp_path):
    data = tmp_path / "data"
    _write_train(data, 2)  # < 3
    assert ft.run_training(_args(data, tmp_path / "adapter")) == ft.RESULT_SKIP
