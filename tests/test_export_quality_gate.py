"""Tests for feedback export quality gate (E20)."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from scripts.export_feedback_jsonl import _is_low_signal_pair, export


def _seed_db(db_path: Path) -> None:
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
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.executemany(
            "INSERT INTO feedback_pairs(inbound_text, edited_reply, rating, edit_distance_pct, used_in_finetune) VALUES(?, ?, ?, ?, 0)",
            [
                ("Can you draft a reply about the project?", "Sure, here's the draft.", 5, 0.10),
                ("Where is the report?", "The report is on Drive.", 4, 0.20),
                ("Ok", "Ok", 3, 0.00),
                ("Thanks", "Thanks", 3, 0.00),
                ("Hi", "Hello", 3, 0.00),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_is_low_signal_pair_filters_acknowledgements() -> None:
    assert _is_low_signal_pair("ok", "ok") is True
    assert _is_low_signal_pair("thanks", "thanks") is True
    assert _is_low_signal_pair("hi", "hello") is True
    assert _is_low_signal_pair("Please send the report", "I'll send the report this afternoon.") is False


def test_export_filters_low_signal_pairs(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _seed_db(db_path)

    output_dir = tmp_path / "data" / "feedback"
    output_dir.mkdir(parents=True)
    train_path = output_dir / "train.jsonl"

    class Args:
        all = False
        since = None
        output = str(train_path)
        min_rating = 1
        min_edit_pct = 0.0
        db = str(db_path)
        no_persona = True
        configs_dir = str(tmp_path)
        dpo = False
        curriculum = False
        no_dedup = True

    with patch("scripts.export_feedback_jsonl.DEFAULT_OUTPUT_DIR", output_dir):
        export(Args())

    valid_path = output_dir / "valid.jsonl"
    assert train_path.exists()
    assert valid_path.exists()

    exported_pairs = []
    for path in (train_path, valid_path):
        with open(path, "r", encoding="utf-8") as f:
            exported_pairs.extend(json.loads(line) for line in f if line.strip())

    inbounds = [p["messages"][0]["content"] for p in exported_pairs if p.get("messages")]
    assert "Can you draft a reply about the project?" in inbounds
    assert "Where is the report?" in inbounds
    assert "Ok" not in inbounds
    assert "Thanks" not in inbounds
    assert "Hi" not in inbounds
