"""Bootstrap fine-tune fixes: a history-only corpus must be trainable.

Two blockers that made the wizard's "Start fine-tuning" silently produce nothing
for a fresh user whose only data is historical sent mail:
  1. The export's edit-distance floor discarded every *organic* pair (real sent
     replies have edit_distance_pct=0 — no draft to diff). Organic pairs are now
     exempt from that floor.
  2. finetune_lora.py left the leading `{"_curriculum": ...}` metadata line in
     train.jsonl, which mlx_lm (>=0.31) rejects as a bad record, aborting on
     line 1. It's now stripped before training.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from scripts.export_feedback_jsonl import export
from scripts.finetune_lora import strip_curriculum_line

# --- Fix 2: curriculum line stripping --------------------------------------


def _write(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_strip_curriculum_line_removes_metadata(tmp_path):
    train = tmp_path / "train.jsonl"
    _write(train, [
        '{"_curriculum": true, "warmup_count": 2, "total": 3}',
        '{"messages": [{"role": "user", "content": "a"}]}',
        '{"messages": [{"role": "user", "content": "b"}]}',
    ])
    assert strip_curriculum_line(train) is True
    remaining = [json.loads(line) for line in train.read_text().splitlines() if line.strip()]
    assert len(remaining) == 2
    assert all("messages" in r for r in remaining)  # first line is now a real record


def test_strip_curriculum_line_noop_without_metadata(tmp_path):
    train = tmp_path / "train.jsonl"
    _write(train, ['{"messages": [{"role": "user", "content": "a"}]}'])
    before = train.read_text()
    assert strip_curriculum_line(train) is False
    assert train.read_text() == before  # unchanged


def test_strip_curriculum_line_is_idempotent(tmp_path):
    train = tmp_path / "train.jsonl"
    _write(train, [
        '{"_curriculum": true}',
        '{"messages": [{"role": "user", "content": "a"}]}',
    ])
    assert strip_curriculum_line(train) is True
    assert strip_curriculum_line(train) is False  # nothing left to strip


def test_strip_curriculum_line_missing_file(tmp_path):
    assert strip_curriculum_line(tmp_path / "nope.jsonl") is False


# --- Fix 1: organic pairs survive the edit-distance floor -------------------


def _seed_db_with_organic(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE feedback_pairs (
                id INTEGER PRIMARY KEY,
                inbound_text TEXT,
                generated_draft TEXT,
                edited_reply TEXT,
                feedback_note TEXT,
                rating INTEGER,
                edit_distance_pct REAL,
                used_in_finetune BOOLEAN DEFAULT 0,
                organic BOOLEAN DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.executemany(
            "INSERT INTO feedback_pairs(inbound_text, edited_reply, rating, edit_distance_pct, organic) VALUES(?, ?, ?, ?, ?)",
            [
                # Organic: real sent replies, edit_pct=0, rating=3 — must survive.
                ("Can you send the Q2 figures when you get a chance?",
                 "Sure, I'll pull the Q2 numbers together and send them this afternoon.", 3, 0.0, 1),
                ("Are we still on for Thursday?",
                 "Yes, Thursday at 2pm works for me — see you then.", 3, 0.0, 1),
                ("Any thoughts on the vendor proposal?",
                 "I think it's solid; let's push back on the timeline though.", 3, 0.0, 1),
                # Non-organic with edit_pct=0, rating<5 — should still be filtered.
                ("Where is the deck?",
                 "The deck is in the shared Drive folder now.", 3, 0.0, 0),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _run_export(tmp_path: Path) -> list[str]:
    db_path = tmp_path / "test.db"
    _seed_db_with_organic(db_path)
    output_dir = tmp_path / "data" / "feedback"
    output_dir.mkdir(parents=True)
    train_path = output_dir / "train.jsonl"

    class Args:
        all = True
        since = None
        output = str(train_path)
        min_rating = 1
        min_edit_pct = 0.05  # floor that would filter edit_pct=0 pairs
        db = str(db_path)
        no_persona = True
        configs_dir = str(tmp_path)
        dpo = False
        curriculum = False
        no_dedup = True

    with patch("scripts.export_feedback_jsonl.DEFAULT_OUTPUT_DIR", output_dir):
        export(Args())

    pairs = []
    for path in (output_dir / "train.jsonl", output_dir / "valid.jsonl"):
        if path.exists():
            with open(path, encoding="utf-8") as f:
                pairs.extend(json.loads(line) for line in f if line.strip())
    return [p["messages"][0]["content"] for p in pairs if p.get("messages")]


def test_export_keeps_organic_pairs_despite_zero_edit(tmp_path):
    inbounds = _run_export(tmp_path)
    # All three organic pairs survive the edit-distance floor...
    assert "Can you send the Q2 figures when you get a chance?" in inbounds
    assert "Are we still on for Thursday?" in inbounds
    assert "Any thoughts on the vendor proposal?" in inbounds
    # ...but the non-organic low-edit pair is still filtered.
    assert "Where is the deck?" not in inbounds
