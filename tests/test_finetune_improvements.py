"""Tests for fine-tuning improvements (Items 1-4)."""

from __future__ import annotations

import json
import sqlite3
from argparse import Namespace
from pathlib import Path

import yaml


# --- Item 1: Rich training data format ---


def test_build_system_message_with_persona(tmp_path):
    """System message includes persona preamble when persona config exists."""
    from scripts.export_feedback_jsonl import _build_system_message

    persona = {
        "style": {"voice": "direct, clear", "avg_reply_words": 40},
        "greeting_patterns": {"internal": "Hi {name},", "default": "Hi,"},
        "closing_patterns": {"informal": "Cheers,", "default": "Best,"},
    }
    prompts = {"system_prompt": "You are YouOS."}
    msg = _build_system_message(persona, prompts)
    assert "You are YouOS." in msg
    assert "Voice style: direct, clear." in msg
    assert "~40 words" in msg
    assert "Greeting patterns:" in msg
    assert "Closing patterns:" in msg


def test_build_system_message_empty_persona():
    """System message falls back gracefully with empty persona."""
    from scripts.export_feedback_jsonl import _build_system_message

    msg = _build_system_message({}, {})
    assert "YouOS" in msg


def test_build_record_with_system():
    """build_record includes system role when system_message provided."""
    from scripts.export_feedback_jsonl import build_record

    rec = build_record("hello", "hi there", system_message="You are YouOS.")
    assert len(rec["messages"]) == 3
    assert rec["messages"][0]["role"] == "system"
    assert rec["messages"][0]["content"] == "You are YouOS."
    assert rec["messages"][1]["role"] == "user"
    assert rec["messages"][2]["role"] == "assistant"


def test_build_record_bare_format():
    """build_record without system_message produces bare format."""
    from scripts.export_feedback_jsonl import build_record

    rec = build_record("hello", "hi there")
    assert len(rec["messages"]) == 2
    assert rec["messages"][0]["role"] == "user"
    assert rec["messages"][1]["role"] == "assistant"


def test_export_with_persona(tmp_path):
    """Export includes system message when persona/prompts configs exist."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE feedback_pairs (id INTEGER PRIMARY KEY, inbound_text TEXT, "
        "generated_draft TEXT, edited_reply TEXT, feedback_note TEXT, rating INTEGER, "
        "used_in_finetune INTEGER DEFAULT 0, edit_distance_pct REAL, reply_pair_id INTEGER, "
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply, rating) "
        "VALUES ('test inbound', 'draft', 'this is a sufficiently long edited reply text', 5)"
    )
    conn.commit()
    conn.close()

    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    (configs_dir / "persona.yaml").write_text(
        yaml.dump({"style": {"voice": "test-voice", "avg_reply_words": 50}})
    )
    (configs_dir / "prompts.yaml").write_text(
        yaml.dump({"system_prompt": "Test system prompt."})
    )

    output = tmp_path / "train.jsonl"
    from scripts.export_feedback_jsonl import export

    args = Namespace(
        all=True, since=None, output=str(output), min_rating=3,
        min_edit_pct=0.05, db=str(db_path), no_persona=False,
        configs_dir=str(configs_dir),
    )
    export(args)

    with open(output) as f:
        rec = json.loads(f.readline())
    assert len(rec["messages"]) == 3
    assert rec["messages"][0]["role"] == "system"
    assert "test-voice" in rec["messages"][0]["content"]


def test_export_no_persona_flag(tmp_path):
    """Export with --no-persona produces bare format."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE feedback_pairs (id INTEGER PRIMARY KEY, inbound_text TEXT, "
        "generated_draft TEXT, edited_reply TEXT, feedback_note TEXT, rating INTEGER, "
        "used_in_finetune INTEGER DEFAULT 0, edit_distance_pct REAL, reply_pair_id INTEGER, "
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply, rating) "
        "VALUES ('test inbound', 'draft', 'this is a sufficiently long edited reply text', 5)"
    )
    conn.commit()
    conn.close()

    output = tmp_path / "train.jsonl"
    from scripts.export_feedback_jsonl import export

    args = Namespace(
        all=True, since=None, output=str(output), min_rating=3,
        min_edit_pct=0.05, db=str(db_path), no_persona=True,
        configs_dir=str(tmp_path / "configs"),
    )
    export(args)

    with open(output) as f:
        rec = json.loads(f.readline())
    assert len(rec["messages"]) == 2
    assert rec["messages"][0]["role"] == "user"


# --- Item 2: Training data quality filter ---


def _create_feedback_db(db_path, rows):
    """Helper: create feedback_pairs table with given rows."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE feedback_pairs (id INTEGER PRIMARY KEY, inbound_text TEXT, "
        "generated_draft TEXT, edited_reply TEXT, feedback_note TEXT, rating INTEGER, "
        "used_in_finetune INTEGER DEFAULT 0, edit_distance_pct REAL, reply_pair_id INTEGER, "
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    for r in rows:
        conn.execute(
            "INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply, rating, edit_distance_pct) "
            "VALUES (?, ?, ?, ?, ?)",
            r,
        )
    conn.commit()
    conn.close()


def test_quality_filter_excludes_low_rating(tmp_path):
    """Pairs with rating < 3 are excluded."""
    db_path = tmp_path / "test.db"
    _create_feedback_db(db_path, [
        ("inbound", "draft", "a long enough edited reply text", 2, 0.3),
        ("inbound", "draft", "a long enough edited reply text", 4, 0.3),
    ])
    output = tmp_path / "train.jsonl"
    from scripts.export_feedback_jsonl import export

    args = Namespace(
        all=True, since=None, output=str(output), min_rating=3,
        min_edit_pct=0.05, db=str(db_path), no_persona=True,
        configs_dir=str(tmp_path),
    )
    export(args)

    lines = output.read_text().strip().split("\n")
    assert len(lines) == 1  # only the rating=4 pair


def test_quality_filter_excludes_short_replies(tmp_path):
    """Pairs with edited_reply < 15 chars are excluded."""
    db_path = tmp_path / "test.db"
    _create_feedback_db(db_path, [
        ("inbound", "draft", "short", 5, 0.3),
        ("inbound", "draft", "a sufficiently long reply text here", 5, 0.3),
    ])
    output = tmp_path / "train.jsonl"
    from scripts.export_feedback_jsonl import export

    args = Namespace(
        all=True, since=None, output=str(output), min_rating=3,
        min_edit_pct=0.05, db=str(db_path), no_persona=True,
        configs_dir=str(tmp_path),
    )
    export(args)

    lines = output.read_text().strip().split("\n")
    assert len(lines) == 1


def test_quality_filter_excludes_low_edit_not_five_star(tmp_path, capsys):
    """Pairs with edit_distance_pct < 0.05 and rating < 5 are excluded."""
    db_path = tmp_path / "test.db"
    _create_feedback_db(db_path, [
        ("inbound", "draft", "a long enough edited reply text", 4, 0.02),  # low edit + not 5-star
        ("inbound", "draft", "a long enough edited reply text", 5, 0.02),  # low edit + 5-star = keep
        ("inbound", "draft", "a long enough edited reply text", 4, 0.10),  # high edit + 4-star = keep
    ])
    output = tmp_path / "train.jsonl"
    from scripts.export_feedback_jsonl import export

    args = Namespace(
        all=True, since=None, output=str(output), min_rating=3,
        min_edit_pct=0.05, db=str(db_path), no_persona=True,
        configs_dir=str(tmp_path),
    )
    export(args)

    captured = capsys.readouterr()
    assert "Exported 2 pairs" in captured.out
    assert "filtered out 1 low-quality pairs" in captured.out


def test_quality_filter_null_rating_included_with_warning(tmp_path, capsys):
    """Null-rated pairs are included with a warning."""
    db_path = tmp_path / "test.db"
    _create_feedback_db(db_path, [
        ("inbound", "draft", "a long enough edited reply text", None, 0.3),
    ])
    output = tmp_path / "train.jsonl"
    from scripts.export_feedback_jsonl import export

    args = Namespace(
        all=True, since=None, output=str(output), min_rating=3,
        min_edit_pct=0.05, db=str(db_path), no_persona=True,
        configs_dir=str(tmp_path),
    )
    export(args)

    lines = output.read_text().strip().split("\n")
    assert len(lines) == 1
    captured = capsys.readouterr()
    assert "null rating" in captured.out


def test_quality_filter_summary_output(tmp_path, capsys):
    """Export prints summary with filtered count."""
    db_path = tmp_path / "test.db"
    _create_feedback_db(db_path, [
        ("inbound", "draft", "a long enough edited reply text", 5, 0.3),
        ("inbound", "draft", "short", 5, 0.3),  # filtered: too short
        ("inbound", "draft", "a long enough edited reply text", 1, 0.3),  # filtered: low rating
    ])
    output = tmp_path / "train.jsonl"
    from scripts.export_feedback_jsonl import export

    args = Namespace(
        all=True, since=None, output=str(output), min_rating=3,
        min_edit_pct=0.05, db=str(db_path), no_persona=True,
        configs_dir=str(tmp_path),
    )
    export(args)

    captured = capsys.readouterr()
    assert "filtered out 2 low-quality pairs" in captured.out
