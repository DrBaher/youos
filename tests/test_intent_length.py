"""Tests for per-intent reply length calibration (Item 4)."""

import json
import sqlite3
import statistics

from app.generation.service import _compute_max_tokens, assemble_prompt


def test_compute_max_tokens_default():
    assert _compute_max_tokens(None) == 300


def test_compute_max_tokens_with_words():
    assert _compute_max_tokens(40) == 200


def test_compute_max_tokens_intent_override():
    persona = {"style": {"intent_avg_words": {"thank_you": 12}}}
    result = _compute_max_tokens(40, persona=persona, intent="thank_you")
    assert result == 100  # max(100, min(500, 12*5)) = 100


def test_compute_max_tokens_intent_fallback():
    persona = {"style": {"intent_avg_words": {"thank_you": 12}}}
    result = _compute_max_tokens(40, persona=persona, intent="meeting_request")
    assert result == 200  # Falls back to 40*5


def test_assemble_prompt_uses_intent_avg_words():
    persona = {
        "style": {
            "voice": "direct",
            "avg_reply_words": 40,
            "constraints": [],
            "intent_avg_words": {"thank_you": 12},
        },
    }
    prompt = assemble_prompt(
        inbound_message="Thanks!",
        reply_pairs=[],
        persona=persona,
        prompts={},
        intent_hint="thank_you",
    )
    assert "~12 words" in prompt


def test_assemble_prompt_falls_back_to_global():
    persona = {
        "style": {
            "voice": "direct",
            "avg_reply_words": 40,
            "constraints": [],
            "intent_avg_words": {"thank_you": 12},
        },
    }
    prompt = assemble_prompt(
        inbound_message="test",
        reply_pairs=[],
        persona=persona,
        prompts={},
        intent_hint="meeting_request",
    )
    assert "~40 words" in prompt


def test_analyze_includes_intent_avg_words(tmp_path):
    """analyze() output includes intent_avg_words."""
    from scripts.analyze_persona import analyze

    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE reply_pairs (
            id INTEGER PRIMARY KEY,
            inbound_text TEXT,
            reply_text TEXT,
            inbound_author TEXT,
            reply_author TEXT,
            metadata_json TEXT,
            source_type TEXT DEFAULT 'email',
            source_id TEXT DEFAULT '',
            document_id INTEGER,
            paired_at TEXT,
            created_ts TEXT DEFAULT CURRENT_TIMESTAMP,
            auto_feedback_processed INTEGER DEFAULT 0,
            quality_score REAL DEFAULT 1.0,
            thread_id TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO reply_pairs (inbound_text, reply_text, inbound_author, metadata_json) VALUES (?, ?, ?, ?)",
        ("Can we schedule a meeting?", "Sure, how about 3pm Tuesday? I can book a room.", "alice@co.com", "{}"),
    )
    conn.execute(
        "INSERT INTO reply_pairs (inbound_text, reply_text, inbound_author, metadata_json) VALUES (?, ?, ?, ?)",
        ("Thanks for your help!", "No problem!", "bob@co.com", "{}"),
    )
    conn.commit()
    conn.close()

    findings = analyze(db)
    assert "intent_avg_words" in findings
    assert isinstance(findings["intent_avg_words"], dict)


def test_merge_intent_avg_words(tmp_path):
    """merge_persona_analysis merges intent_avg_words into persona.yaml."""
    import yaml

    from scripts.analyze_persona_merge import merge_persona_analysis

    persona_path = tmp_path / "persona.yaml"
    persona_path.write_text(yaml.dump({"style": {"voice": "direct", "avg_reply_words": 40}}))

    findings = {"intent_avg_words": {"meeting_request": 45, "thank_you": 12}}
    changes = merge_persona_analysis(
        persona_path=persona_path,
        log_path=tmp_path / "merge.log",
        findings_dict=findings,
    )
    assert any("intent_avg_words" in c for c in changes)

    merged = yaml.safe_load(persona_path.read_text())
    assert merged["style"]["intent_avg_words"]["meeting_request"] == 45
    assert merged["style"]["intent_avg_words"]["thank_you"] == 12
