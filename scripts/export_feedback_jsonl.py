"""Export feedback pairs to JSONL for MLX chat fine-tuning."""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT_DIR / "var" / "youos.db"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "data" / "feedback"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export feedback pairs to JSONL")
    p.add_argument("--all", action="store_true", help="Export all pairs, not just unused")
    p.add_argument("--since", type=str, default=None, help="Only pairs created after this date (YYYY-MM-DD)")
    p.add_argument("--output", type=str, default=None, help="Output file path (default: data/feedback/train.jsonl)")
    p.add_argument("--min-rating", type=int, default=None, help="Minimum rating to include")
    p.add_argument("--db", type=str, default=str(DEFAULT_DB), help="Database path")
    return p.parse_args()


def export(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found at {db_path}")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        query = "SELECT inbound_text, edited_reply FROM feedback_pairs WHERE 1=1"
        params: list = []

        if not args.all:
            query += " AND used_in_finetune = 0"

        if args.since:
            query += " AND created_at >= ?"
            params.append(args.since)

        if args.min_rating is not None:
            query += " AND rating >= ?"
            params.append(args.min_rating)

        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    if not rows:
        print("No matching feedback pairs found. Exported 0 pairs.")
        return

    # Build JSONL records
    records = []
    for row in rows:
        records.append(
            {
                "messages": [
                    {"role": "user", "content": row["inbound_text"]},
                    {"role": "assistant", "content": row["edited_reply"]},
                ]
            }
        )

    # Shuffle and split 90/10
    random.shuffle(records)
    split_idx = max(1, int(len(records) * 0.9))
    train = records[:split_idx]
    valid = records[split_idx:] if len(records) > 1 else []

    # Determine output paths
    if args.output:
        train_path = Path(args.output)
        valid_path = train_path.parent / "valid.jsonl"
    else:
        output_dir = DEFAULT_OUTPUT_DIR
        output_dir.mkdir(parents=True, exist_ok=True)
        train_path = output_dir / "train.jsonl"
        valid_path = output_dir / "valid.jsonl"

    train_path.parent.mkdir(parents=True, exist_ok=True)

    with open(train_path, "w", encoding="utf-8") as f:
        for rec in train:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    with open(valid_path, "w", encoding="utf-8") as f:
        for rec in valid:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"Exported {len(records)} pairs to {train_path}")
    print(f"  Train: {len(train)} pairs -> {train_path}")
    print(f"  Valid: {len(valid)} pairs -> {valid_path}")


def main() -> None:
    args = parse_args()
    export(args)


if __name__ == "__main__":
    main()
