"""Tests for DPO preference pair export."""

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

from scripts.export_feedback_jsonl import export_dpo


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
            edited_reply TEXT,
            rating INTEGER,
            edit_distance_pct REAL DEFAULT 0.1,
            created_at TEXT DEFAULT '',
            used_in_finetune INTEGER DEFAULT 0,
            reply_pair_id INTEGER,
            generated_draft TEXT DEFAULT '',
            feedback_note TEXT DEFAULT ''
        )
    """)
    for inbound, reply, rating in pairs:
        conn.execute(
            "INSERT INTO feedback_pairs (inbound_text, edited_reply, rating) VALUES (?, ?, ?)",
            (inbound, reply, rating),
        )
    conn.commit()
    conn.close()
    return db_path


def test_dpo_export_creates_pairs(tmp_path):
    db_path = _make_db(
        [
            ("Can we schedule a meeting to discuss the project timeline?", "Sure, let me check my calendar and get back to you.", 5),
            ("Can you review my proposal for the new feature set?", "ok i guess whatever no opinion here", 1),
        ]
    )
    args = argparse.Namespace(db=str(db_path), dpo=True)

    # Monkey-patch ROOT_DIR for output
    import scripts.export_feedback_jsonl as mod

    orig_root = mod.ROOT_DIR
    mod.ROOT_DIR = tmp_path
    try:
        export_dpo(args)
    finally:
        mod.ROOT_DIR = orig_root

    output = tmp_path / "data" / "dpo_train.jsonl"
    assert output.exists()
    lines = output.read_text().strip().split("\n")
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert "prompt" in record
    assert "chosen" in record
    assert "rejected" in record
    assert record["chosen"] == "Sure, let me check my calendar and get back to you."
    assert record["rejected"] == "ok i guess whatever no opinion here"

    db_path.unlink()


def test_dpo_export_no_rejected(tmp_path, capsys):
    """No DPO pairs when there are no rejected examples."""
    db_path = _make_db(
        [
            ("Hello there", "Hi! How can I help?", 5),
            ("Another one", "Sure thing!", 4),
        ]
    )
    args = argparse.Namespace(db=str(db_path), dpo=True)

    import scripts.export_feedback_jsonl as mod

    orig_root = mod.ROOT_DIR
    mod.ROOT_DIR = tmp_path
    try:
        export_dpo(args)
    finally:
        mod.ROOT_DIR = orig_root

    output = tmp_path / "data" / "dpo_train.jsonl"
    assert not output.exists()
    captured = capsys.readouterr()
    assert "Not enough DPO pairs" in captured.out

    db_path.unlink()


def test_dpo_length_matching(tmp_path):
    """DPO pairs must match on inbound length (within 50%)."""
    db_path = _make_db(
        [
            ("Short msg", "Good reply here with details", 5),
            ("This is a much much much much longer inbound message that goes on", "this is a poor quality response", 1),
        ]
    )
    args = argparse.Namespace(db=str(db_path), dpo=True)

    import scripts.export_feedback_jsonl as mod

    orig_root = mod.ROOT_DIR
    mod.ROOT_DIR = tmp_path
    try:
        export_dpo(args)
    finally:
        mod.ROOT_DIR = orig_root

    output = tmp_path / "data" / "dpo_train.jsonl"
    # Should not match because inbound lengths differ too much
    assert not output.exists()

    db_path.unlink()
