"""Tests for persona confidence intervals (Item 12)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.analyze_persona import analyze


def _create_db(tmp_path: Path, word_counts: list[int]) -> Path:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE reply_pairs (
            id INTEGER PRIMARY KEY,
            inbound_text TEXT,
            reply_text TEXT,
            inbound_author TEXT,
            reply_author TEXT DEFAULT 'user',
            metadata_json TEXT DEFAULT '{}',
            paired_at TEXT
        )
    """)
    for i, wc in enumerate(word_counts):
        reply = " ".join(["word"] * wc)
        conn.execute(
            "INSERT INTO reply_pairs (inbound_text, reply_text, inbound_author) VALUES (?, ?, ?)",
            (f"inbound {i}", reply, f"sender{i}@test.com"),
        )
    conn.commit()
    conn.close()
    return db_path


def test_confidence_intervals_present(tmp_path):
    """Findings should include p25, p75, and stddev."""
    db = _create_db(tmp_path, [10, 20, 30, 40, 50])
    findings = analyze(db)
    assert "avg_reply_words_p25" in findings
    assert "avg_reply_words_p75" in findings
    assert "avg_reply_words_stddev" in findings


def test_p25_less_than_p75(tmp_path):
    """p25 should be <= p75."""
    db = _create_db(tmp_path, [5, 10, 15, 20, 25, 30, 35, 40])
    findings = analyze(db)
    assert findings["avg_reply_words_p25"] <= findings["avg_reply_words_p75"]


def test_stddev_positive(tmp_path):
    """Stddev should be positive when word counts vary."""
    db = _create_db(tmp_path, [5, 50, 100])
    findings = analyze(db)
    assert findings["avg_reply_words_stddev"] > 0


def test_stddev_zero_single_pair(tmp_path):
    """Stddev should be 0 with single pair."""
    db = _create_db(tmp_path, [20])
    findings = analyze(db)
    assert findings["avg_reply_words_stddev"] == 0.0


def test_reply_length_includes_p25_p75(tmp_path):
    """reply_length dict should also have p25 and p75."""
    db = _create_db(tmp_path, [10, 20, 30])
    findings = analyze(db)
    assert findings["reply_length"]["p25"] == findings["avg_reply_words_p25"]
    assert findings["reply_length"]["p75"] == findings["avg_reply_words_p75"]


def test_assemble_prompt_with_percentiles():
    """Prompt should include typical range when p25/p75 available."""
    from app.generation.service import assemble_prompt

    persona = {
        "style": {
            "voice": "direct",
            "avg_reply_words": 40,
            "avg_reply_words_p25": 20,
            "avg_reply_words_p75": 60,
            "constraints": [],
        }
    }
    prompt = assemble_prompt(
        inbound_message="test",
        reply_pairs=[],
        persona=persona,
        prompts={},
    )
    assert "typical range: 20" in prompt
    assert "60" in prompt


def test_assemble_prompt_without_percentiles():
    """Without p25/p75, prompt should not mention typical range."""
    from app.generation.service import assemble_prompt

    persona = {
        "style": {
            "voice": "direct",
            "avg_reply_words": 40,
            "constraints": [],
        }
    }
    prompt = assemble_prompt(
        inbound_message="test",
        reply_pairs=[],
        persona=persona,
        prompts={},
    )
    assert "typical range" not in prompt
    assert "~40 words" in prompt
