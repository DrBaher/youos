"""Tests for fine-tuning improvements (Items 1-4)."""

from __future__ import annotations

import json
import sqlite3
from argparse import Namespace

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
    (configs_dir / "persona.yaml").write_text(yaml.dump({"style": {"voice": "test-voice", "avg_reply_words": 50}}))
    (configs_dir / "prompts.yaml").write_text(yaml.dump({"system_prompt": "Test system prompt."}))

    output = tmp_path / "train.jsonl"
    from scripts.export_feedback_jsonl import export

    args = Namespace(
        all=True,
        since=None,
        output=str(output),
        min_rating=3,
        min_edit_pct=0.05,
        db=str(db_path),
        no_persona=False,
        configs_dir=str(configs_dir),
        curriculum=False,
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
        all=True,
        since=None,
        output=str(output),
        min_rating=3,
        min_edit_pct=0.05,
        db=str(db_path),
        no_persona=True,
        configs_dir=str(tmp_path / "configs"),
        curriculum=False,
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
            "INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply, rating, edit_distance_pct) VALUES (?, ?, ?, ?, ?)",
            r,
        )
    conn.commit()
    conn.close()


def test_quality_filter_excludes_low_rating(tmp_path):
    """Pairs with rating < 3 are excluded."""
    db_path = tmp_path / "test.db"
    _create_feedback_db(
        db_path,
        [
            ("inbound", "draft", "a long enough edited reply text", 2, 0.3),
            ("inbound", "draft", "a long enough edited reply text", 4, 0.3),
        ],
    )
    output = tmp_path / "train.jsonl"
    from scripts.export_feedback_jsonl import export

    args = Namespace(
        all=True,
        since=None,
        output=str(output),
        min_rating=3,
        min_edit_pct=0.05,
        db=str(db_path),
        no_persona=True,
        configs_dir=str(tmp_path),
        curriculum=False,
    )
    export(args)

    lines = output.read_text().strip().split("\n")
    assert len(lines) == 1  # only the rating=4 pair


def test_quality_filter_excludes_short_replies(tmp_path):
    """Pairs with edited_reply < 15 chars are excluded."""
    db_path = tmp_path / "test.db"
    _create_feedback_db(
        db_path,
        [
            ("inbound", "draft", "short", 5, 0.3),
            ("inbound", "draft", "a sufficiently long reply text here", 5, 0.3),
        ],
    )
    output = tmp_path / "train.jsonl"
    from scripts.export_feedback_jsonl import export

    args = Namespace(
        all=True,
        since=None,
        output=str(output),
        min_rating=3,
        min_edit_pct=0.05,
        db=str(db_path),
        no_persona=True,
        configs_dir=str(tmp_path),
        curriculum=False,
    )
    export(args)

    lines = output.read_text().strip().split("\n")
    assert len(lines) == 1


def test_quality_filter_excludes_low_edit_not_five_star(tmp_path, capsys):
    """Pairs with edit_distance_pct < 0.05 and rating < 5 are excluded."""
    db_path = tmp_path / "test.db"
    _create_feedback_db(
        db_path,
        [
            ("inbound", "draft", "a long enough edited reply text", 4, 0.02),  # low edit + not 5-star
            ("inbound", "draft", "a long enough edited reply text", 5, 0.02),  # low edit + 5-star = keep
            ("inbound", "draft", "a long enough edited reply text", 4, 0.10),  # high edit + 4-star = keep
        ],
    )
    output = tmp_path / "train.jsonl"
    from scripts.export_feedback_jsonl import export

    args = Namespace(
        all=True,
        since=None,
        output=str(output),
        min_rating=3,
        min_edit_pct=0.05,
        db=str(db_path),
        no_persona=True,
        configs_dir=str(tmp_path),
        curriculum=False,
    )
    export(args)

    captured = capsys.readouterr()
    # E15 oversampling may increase the final count; check filtered count is correct
    assert "filtered out 1 low-quality pairs" in captured.out
    assert "Exported" in captured.out  # some pairs were exported


def test_quality_filter_null_rating_included_with_warning(tmp_path, capsys):
    """Null-rated pairs are included with a warning."""
    db_path = tmp_path / "test.db"
    _create_feedback_db(
        db_path,
        [
            ("inbound", "draft", "a long enough edited reply text", None, 0.3),
        ],
    )
    output = tmp_path / "train.jsonl"
    from scripts.export_feedback_jsonl import export

    args = Namespace(
        all=True,
        since=None,
        output=str(output),
        min_rating=3,
        min_edit_pct=0.05,
        db=str(db_path),
        no_persona=True,
        configs_dir=str(tmp_path),
        curriculum=False,
    )
    export(args)

    lines = output.read_text().strip().split("\n")
    assert len(lines) == 1
    captured = capsys.readouterr()
    assert "null rating" in captured.out


def test_quality_filter_summary_output(tmp_path, capsys):
    """Export prints summary with filtered count."""
    db_path = tmp_path / "test.db"
    _create_feedback_db(
        db_path,
        [
            ("inbound", "draft", "a long enough edited reply text", 5, 0.3),
            ("inbound", "draft", "short", 5, 0.3),  # filtered: too short
            ("inbound", "draft", "a long enough edited reply text", 1, 0.3),  # filtered: low rating
        ],
    )
    output = tmp_path / "train.jsonl"
    from scripts.export_feedback_jsonl import export

    args = Namespace(
        all=True,
        since=None,
        output=str(output),
        min_rating=3,
        min_edit_pct=0.05,
        db=str(db_path),
        no_persona=True,
        configs_dir=str(tmp_path),
        curriculum=False,
    )
    export(args)

    captured = capsys.readouterr()
    assert "filtered out 2 low-quality pairs" in captured.out


# --- Item 3: Temporal train/validation split ---


def _create_feedback_db_with_dates(db_path, rows):
    """Helper: create feedback_pairs with created_at dates."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE feedback_pairs (id INTEGER PRIMARY KEY, inbound_text TEXT, "
        "generated_draft TEXT, edited_reply TEXT, feedback_note TEXT, rating INTEGER, "
        "used_in_finetune INTEGER DEFAULT 0, edit_distance_pct REAL, reply_pair_id INTEGER, "
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    for inbound, draft, reply, rating, edit_pct, created_at in rows:
        conn.execute(
            "INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply, rating, edit_distance_pct, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (inbound, draft, reply, rating, edit_pct, created_at),
        )
    conn.commit()
    conn.close()


def test_temporal_split_most_recent_in_validation(tmp_path, capsys):
    """Most recent pairs end up in the validation set."""
    db_path = tmp_path / "test.db"
    # 10 pairs with increasing dates — val should be last 15% = 1-2 pairs
    rows = [(f"inbound {i}", "draft", f"a long enough reply text {i}", 5, 0.3, f"2026-03-{i + 1:02d}T00:00:00") for i in range(10)]
    _create_feedback_db_with_dates(db_path, rows)

    train_path = tmp_path / "train.jsonl"
    valid_path = tmp_path / "valid.jsonl"
    from scripts.export_feedback_jsonl import export

    args = Namespace(
        all=True,
        since=None,
        output=str(train_path),
        min_rating=3,
        min_edit_pct=0.05,
        db=str(db_path),
        no_persona=True,
        configs_dir=str(tmp_path),
        curriculum=False,
    )
    export(args)

    train_lines = train_path.read_text().strip().split("\n")
    valid_lines = valid_path.read_text().strip().split("\n")

    # 15% of 10 = 1.5 -> max(1, min(20, 1)) = 1
    assert len(valid_lines) >= 1
    assert len(train_lines) + len(valid_lines) == 10

    # Validation should contain the most recent pair (reply text 9)
    last_valid = json.loads(valid_lines[-1])
    assert "reply text 9" in last_valid["messages"][-1]["content"]

    captured = capsys.readouterr()
    assert "temporal split" in captured.out


def test_temporal_split_single_pair(tmp_path, capsys):
    """Single pair goes to train, nothing to valid."""
    db_path = tmp_path / "test.db"
    _create_feedback_db_with_dates(
        db_path,
        [
            ("inbound", "draft", "a long enough reply text here", 5, 0.3, "2026-03-01T00:00:00"),
        ],
    )
    train_path = tmp_path / "train.jsonl"
    valid_path = tmp_path / "valid.jsonl"
    from scripts.export_feedback_jsonl import export

    args = Namespace(
        all=True,
        since=None,
        output=str(train_path),
        min_rating=3,
        min_edit_pct=0.05,
        db=str(db_path),
        no_persona=True,
        configs_dir=str(tmp_path),
        curriculum=False,
    )
    export(args)

    assert len(train_path.read_text().strip().split("\n")) == 1
    # Valid file is empty or has 0 lines
    valid_content = valid_path.read_text().strip()
    assert valid_content == ""


def test_temporal_split_val_capped_at_20(tmp_path, capsys):
    """Validation set is capped at 20 even for large datasets."""
    db_path = tmp_path / "test.db"
    rows = [(f"inbound {i}", "draft", f"a long enough reply text {i}", 5, 0.3, f"2026-01-{(i % 28) + 1:02d}T00:00:00") for i in range(200)]
    _create_feedback_db_with_dates(db_path, rows)

    train_path = tmp_path / "train.jsonl"
    valid_path = tmp_path / "valid.jsonl"
    from scripts.export_feedback_jsonl import export

    args = Namespace(
        all=True,
        since=None,
        output=str(train_path),
        min_rating=3,
        min_edit_pct=0.05,
        db=str(db_path),
        no_persona=True,
        configs_dir=str(tmp_path),
        curriculum=False,
    )
    export(args)

    valid_lines = valid_path.read_text().strip().split("\n")
    assert len(valid_lines) == 20
