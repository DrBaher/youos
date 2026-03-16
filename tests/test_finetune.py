"""Tests for LoRA fine-tuning auto-scaling (Item 4)."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from scripts.finetune_lora import compute_auto_config, count_jsonl_lines


def test_auto_config_small_dataset():
    """Small dataset gets more iters per example, higher LR."""
    config = compute_auto_config(10)
    assert config["iters"] == 50  # max(50, 10*3) = 50
    assert config["num_layers"] == 8
    assert config["learning_rate"] == 5e-5


def test_auto_config_medium_dataset():
    """Medium dataset scales iters linearly."""
    config = compute_auto_config(50)
    assert config["iters"] == 150  # min(300, max(50, 50*3)) = 150
    assert config["num_layers"] == 8
    assert config["learning_rate"] == 1e-5


def test_auto_config_large_dataset():
    """Large dataset gets more layers, capped iters."""
    config = compute_auto_config(200)
    assert config["iters"] == 300  # min(300, max(50, 200*3)) = 300
    assert config["num_layers"] == 16
    assert config["learning_rate"] == 1e-5


def test_auto_config_boundary_100():
    """At exactly 100 pairs, switch to 16 layers."""
    config = compute_auto_config(100)
    assert config["num_layers"] == 16
    assert config["iters"] == 300
    assert config["learning_rate"] == 1e-5


def test_auto_config_boundary_20():
    """At exactly 20 pairs, switch to lower LR."""
    config = compute_auto_config(20)
    assert config["learning_rate"] == 1e-5
    assert config["iters"] == 60


def test_count_jsonl_lines(tmp_path):
    """count_jsonl_lines returns correct count."""
    path = tmp_path / "test.jsonl"
    path.write_text('{"a":1}\n{"b":2}\n{"c":3}\n')
    assert count_jsonl_lines(path) == 3


def test_count_jsonl_lines_missing(tmp_path):
    """count_jsonl_lines returns 0 for missing file."""
    assert count_jsonl_lines(tmp_path / "missing.jsonl") == 0


def test_dry_run_shows_auto_config(tmp_path, capsys):
    """Dry run with auto-scaling prints the computed config."""
    from scripts.finetune_lora import run_training

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    train = data_dir / "train.jsonl"
    train.write_text('{"m":1}\n' * 30)

    args = Namespace(
        iters=None, num_layers=None, learning_rate=None, auto=True,
        data_dir=str(data_dir), adapter_dir=str(tmp_path / "adapter"),
        db=str(tmp_path / "test.db"), dry_run=True,
    )
    run_training(args)

    captured = capsys.readouterr()
    assert "auto_scaled: True" in captured.out
    assert "iters: 90" in captured.out  # max(50, 30*3) = 90
    assert "num_layers: 8" in captured.out


def test_cli_override_with_auto(tmp_path, capsys):
    """CLI flags override auto-scaled values."""
    from scripts.finetune_lora import run_training

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    train = data_dir / "train.jsonl"
    train.write_text('{"m":1}\n' * 30)

    args = Namespace(
        iters=200, num_layers=4, learning_rate=3e-4, auto=True,
        data_dir=str(data_dir), adapter_dir=str(tmp_path / "adapter"),
        db=str(tmp_path / "test.db"), dry_run=True,
    )
    run_training(args)

    captured = capsys.readouterr()
    assert "iters: 200" in captured.out
    assert "num_layers: 4" in captured.out
    assert "learning_rate: 0.0003" in captured.out


def test_no_auto_uses_defaults(tmp_path, capsys):
    """--no-auto uses fixed defaults."""
    from scripts.finetune_lora import run_training

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    train = data_dir / "train.jsonl"
    train.write_text('{"m":1}\n' * 30)

    args = Namespace(
        iters=None, num_layers=None, learning_rate=None, auto=False,
        data_dir=str(data_dir), adapter_dir=str(tmp_path / "adapter"),
        db=str(tmp_path / "test.db"), dry_run=True,
    )
    run_training(args)

    captured = capsys.readouterr()
    assert "iters: 100" in captured.out
    assert "num_layers: 8" in captured.out
    assert "auto_scaled: False" in captured.out
