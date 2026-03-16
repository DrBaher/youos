"""Export feedback pairs to JSONL for MLX chat fine-tuning."""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
from pathlib import Path

import yaml

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT_DIR / "var" / "youos.db"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "data" / "feedback"
CONFIGS_DIR = ROOT_DIR / "configs"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export feedback pairs to JSONL")
    p.add_argument("--all", action="store_true", help="Export all pairs, not just unused")
    p.add_argument("--since", type=str, default=None, help="Only pairs created after this date (YYYY-MM-DD)")
    p.add_argument("--output", type=str, default=None, help="Output file path (default: data/feedback/train.jsonl)")
    p.add_argument("--min-rating", type=int, default=None, help="Minimum rating to include")
    p.add_argument("--db", type=str, default=str(DEFAULT_DB), help="Database path")
    p.add_argument("--no-persona", action="store_true", help="Use bare format without persona/system prompt")
    p.add_argument("--configs-dir", type=str, default=str(CONFIGS_DIR), help="Configs directory")
    return p.parse_args()


def _load_persona(configs_dir: Path) -> dict:
    path = configs_dir / "persona.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_prompts(configs_dir: Path) -> dict:
    path = configs_dir / "prompts.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _build_system_message(persona: dict, prompts: dict) -> str:
    """Build a system message combining system_prompt and persona preamble."""
    system_prompt = prompts.get("system_prompt", "You are YouOS, a local-first personal email copilot.").strip()

    style = persona.get("style", {})
    voice = style.get("voice")
    avg_words = style.get("avg_reply_words")
    greeting_patterns = persona.get("greeting_patterns", {})
    closing_patterns = persona.get("closing_patterns", {})

    preamble_parts: list[str] = []
    if voice:
        preamble_parts.append(f"Voice style: {voice}.")
    if avg_words:
        preamble_parts.append(f"Target reply length: ~{avg_words} words.")
    if greeting_patterns:
        greetings = ", ".join(f"{k}: {v}" for k, v in greeting_patterns.items() if k != "default")
        if greetings:
            preamble_parts.append(f"Greeting patterns: {greetings}.")
    if closing_patterns:
        closings = ", ".join(f"{k}: {v}" for k, v in closing_patterns.items() if k != "default")
        if closings:
            preamble_parts.append(f"Closing patterns: {closings}.")

    if preamble_parts:
        return system_prompt + "\n\n" + "\n".join(preamble_parts)
    return system_prompt


def build_record(
    inbound: str,
    edited_reply: str,
    *,
    system_message: str | None = None,
) -> dict:
    """Build a JSONL record with optional system message."""
    messages = []
    if system_message:
        messages.append({"role": "system", "content": system_message})
    messages.append({"role": "user", "content": inbound})
    messages.append({"role": "assistant", "content": edited_reply})
    return {"messages": messages}


def export(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found at {db_path}")
        return

    configs_dir = Path(args.configs_dir)

    # Build system message from persona + prompts (unless --no-persona)
    system_message = None
    if not args.no_persona:
        persona = _load_persona(configs_dir)
        prompts = _load_prompts(configs_dir)
        system_message = _build_system_message(persona, prompts)

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
            build_record(
                row["inbound_text"],
                row["edited_reply"],
                system_message=system_message,
            )
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
