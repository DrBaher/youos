"""LoRA fine-tuning script using mlx_lm on exported feedback pairs."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import get_base_model

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT_DIR / "var" / "youos.db"
DEFAULT_DATA_DIR = ROOT_DIR / "data" / "feedback"
DEFAULT_ADAPTER_DIR = ROOT_DIR / "models" / "adapters" / "latest"
BASE_MODEL = get_base_model()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="LoRA fine-tuning with mlx_lm")
    p.add_argument("--iters", type=int, default=100, help="Training iterations (default: 100)")
    p.add_argument("--data-dir", type=str, default=str(DEFAULT_DATA_DIR), help="Directory with train.jsonl/valid.jsonl")
    p.add_argument("--adapter-dir", type=str, default=str(DEFAULT_ADAPTER_DIR), help="Output adapter directory")
    p.add_argument("--db", type=str, default=str(DEFAULT_DB), help="Database path")
    p.add_argument("--dry-run", action="store_true", help="Show config without training")
    return p.parse_args()


def count_jsonl_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with open(path, encoding="utf-8") as f:
        return sum(1 for _ in f)


def run_training(args: argparse.Namespace) -> None:
    data_dir = Path(args.data_dir)
    adapter_dir = Path(args.adapter_dir)
    train_path = data_dir / "train.jsonl"
    valid_path = data_dir / "valid.jsonl"

    train_count = count_jsonl_lines(train_path)
    valid_count = count_jsonl_lines(valid_path)

    config = {
        "base_model": BASE_MODEL,
        "data_dir": str(data_dir),
        "adapter_dir": str(adapter_dir),
        "iters": args.iters,
        "batch_size": 1,
        "num_layers": 8,
        "learning_rate": 1e-5,
        "train_pairs": train_count,
        "valid_pairs": valid_count,
    }

    print("LoRA fine-tuning config:")
    for k, v in config.items():
        print(f"  {k}: {v}")

    if args.dry_run:
        print("\n--dry-run: exiting without training.")
        return

    if not train_path.exists():
        print(f"\nError: {train_path} not found. Run export_feedback_jsonl.py first.")
        return

    if train_count < 3:
        print(f"\nError: only {train_count} training pairs. Need at least 3.")
        return

    adapter_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "mlx_lm", "lora",
        "--model", BASE_MODEL,
        "--train",
        "--data", str(data_dir),
        "--adapter-path", str(adapter_dir),
        "--iters", str(args.iters),
        "--batch-size", "1",
        "--num-layers", "8",
        "--learning-rate", "1e-5",
    ]

    if valid_path.exists() and valid_count > 0:
        cmd.extend(["--val-batches", "1"])

    print(f"\nRunning: {' '.join(cmd)}\n")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)

    if result.returncode != 0:
        print(f"Training failed (exit {result.returncode}):")
        print(result.stderr)
        return

    print(result.stdout)

    # Parse val loss from output
    val_loss = None
    for line in result.stdout.splitlines():
        m = re.search(r"Val loss[:\s]+([\d.]+)", line, re.IGNORECASE)
        if m:
            val_loss = float(m.group(1))

    # Mark feedback pairs as used
    db_path = Path(args.db)
    pairs_used = 0
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.execute(
                "UPDATE feedback_pairs SET used_in_finetune = 1 WHERE used_in_finetune = 0"
            )
            pairs_used = cursor.rowcount
            conn.commit()
        finally:
            conn.close()

    # Save metadata
    meta = {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "base_model": BASE_MODEL,
        "pairs_used": pairs_used or train_count,
        "iters": args.iters,
        "final_val_loss": val_loss,
    }
    meta_path = adapter_dir / "meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    print(f"\nTraining complete.")
    print(f"  Adapter saved to: {adapter_dir}")
    print(f"  Pairs used: {meta['pairs_used']}")
    print(f"  Val loss: {val_loss}")
    print(f"  Metadata: {meta_path}")


def main() -> None:
    args = parse_args()
    run_training(args)


if __name__ == "__main__":
    main()
