"""Tests for curriculum learning in export and finetune."""

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

from scripts.export_feedback_jsonl import export


def _make_db(pairs: list[tuple[str, str, int]]) -> Path:
    """Create a temp DB with feedback_pairs."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = Path(f.name)
    f.close()
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE feedback_pairs (
            id INTEGER PRIMARY KEY,
            inbound_text TEXT,
            generated_draft TEXT DEFAULT 'draft',
            edited_reply TEXT,
            feedback_note TEXT DEFAULT '',
            rating INTEGER,
            edit_distance_pct REAL DEFAULT 0.1,
            created_at TEXT DEFAULT '',
            used_in_finetune INTEGER DEFAULT 0,
            reply_pair_id INTEGER
        )
    """)
    for i, (inbound, reply, rating) in enumerate(pairs):
        conn.execute(
            "INSERT INTO feedback_pairs (inbound_text, edited_reply, rating, created_at) VALUES (?, ?, ?, ?)",
            (inbound, reply, rating, f"2025-01-{i + 1:02d}T00:00:00Z"),
        )
    conn.commit()
    conn.close()
    return db_path


def test_curriculum_metadata_line(tmp_path):
    """Curriculum export prepends a metadata line."""
    db_path = _make_db(
        [
            ("Hello there, how are you doing today?", "I am doing well, thanks for asking!", 5),
            ("Can you help me with the report analysis?", "Sure, I will take a look right away.", 4),
            ("Please review the attached document asap", "Will review and get back to you soon.", 3),
            ("What is the status of the project work?", "We are on track for the deadline date.", 5),
            ("Are you available for a meeting tomorrow?", "Yes, afternoon works well for me today.", 4),
        ]
    )

    args = argparse.Namespace(
        db=str(db_path),
        all=True,
        since=None,
        output=str(tmp_path / "train.jsonl"),
        min_rating=3,
        min_edit_pct=0.0,
        no_persona=True,
        configs_dir=str(Path(__file__).resolve().parents[1] / "configs"),
        curriculum=True,
        dpo=False,
    )
    export(args)

    train_path = tmp_path / "train.jsonl"
    assert train_path.exists()

    lines = train_path.read_text().strip().split("\n")
    first = json.loads(lines[0])
    assert first.get("_curriculum") is True
    assert "warmup_count" in first
    assert "total" in first

    db_path.unlink()


def test_curriculum_disabled(tmp_path):
    """When --no-curriculum, no metadata line is prepended."""
    db_path = _make_db(
        [
            ("Hello there, how are you doing today?", "I am doing well, thanks for asking!", 5),
            ("Can you help me with the report analysis?", "Sure, I will take a look right away.", 4),
            ("Please review the attached document asap", "Will review and get back to you soon.", 3),
        ]
    )

    args = argparse.Namespace(
        db=str(db_path),
        all=True,
        since=None,
        output=str(tmp_path / "train.jsonl"),
        min_rating=3,
        min_edit_pct=0.0,
        no_persona=True,
        configs_dir=str(Path(__file__).resolve().parents[1] / "configs"),
        curriculum=False,
        dpo=False,
    )
    export(args)

    train_path = tmp_path / "train.jsonl"
    assert train_path.exists()

    lines = train_path.read_text().strip().split("\n")
    first = json.loads(lines[0])
    assert "_curriculum" not in first

    db_path.unlink()


def test_finetune_detects_curriculum(tmp_path):
    """finetune_lora.py count_jsonl_lines counts correctly."""
    from scripts.finetune_lora import count_jsonl_lines

    # Write a file with curriculum metadata line
    train_path = tmp_path / "train.jsonl"
    lines = [
        json.dumps({"_curriculum": True, "warmup_count": 2, "total": 5}),
        json.dumps({"messages": [{"role": "user", "content": "hi"}]}),
        json.dumps({"messages": [{"role": "user", "content": "bye"}]}),
    ]
    train_path.write_text("\n".join(lines) + "\n")

    count = count_jsonl_lines(train_path)
    assert count == 3  # count_jsonl_lines counts all lines
