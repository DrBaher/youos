"""Tests for persona analysis."""

import sqlite3
import tempfile
from pathlib import Path

from scripts.analyze_persona import analyze, strip_signature


def test_strip_signature_with_best():
    text = "Hello there.\n\nBest,\nAlice"
    result = strip_signature(text)
    assert result == "Hello there."


def test_strip_signature_with_cheers():
    text = "Sounds good, let's do it.\n\nCheers,\nBob"
    result = strip_signature(text)
    assert result == "Sounds good, let's do it."


def test_strip_signature_no_signature():
    text = "Just a plain reply with no closing."
    result = strip_signature(text)
    assert result == text


def test_analyze_empty_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE reply_pairs (
            id INTEGER PRIMARY KEY,
            inbound_text TEXT,
            reply_text TEXT,
            inbound_author TEXT,
            reply_author TEXT,
            metadata_json TEXT DEFAULT '{}'
        )
    """)
    conn.commit()
    conn.close()

    findings = analyze(db_path)
    assert findings["total_pairs"] == 0
    assert "error" in findings

    db_path.unlink()


def test_analyze_with_data():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE reply_pairs (
            id INTEGER PRIMARY KEY,
            inbound_text TEXT,
            reply_text TEXT,
            inbound_author TEXT,
            reply_author TEXT,
            metadata_json TEXT DEFAULT '{}'
        )
    """)
    conn.execute(
        "INSERT INTO reply_pairs (reply_text, inbound_author, reply_author) VALUES (?, ?, ?)",
        ("Hi John, sounds good. Let's go with option A.", "john@example.com", "me@example.com"),
    )
    conn.execute(
        "INSERT INTO reply_pairs (reply_text, inbound_author, reply_author) VALUES (?, ?, ?)",
        ("Hey! Yes, Saturday works for me.", "friend@gmail.com", "me@example.com"),
    )
    conn.commit()
    conn.close()

    findings = analyze(db_path)
    assert findings["total_pairs"] == 2
    assert "reply_length" in findings
    assert findings["reply_length"]["avg_words"] > 0

    # New style metrics
    assert "sentence_length_avg" in findings
    assert "bullet_point_pct" in findings
    assert "question_frequency" in findings
    assert "hedge_word_pct" in findings
    assert "directness_score" in findings
    assert "emoji_pct" in findings
    assert "avg_paragraphs" in findings
    assert findings["directness_score"] == round(1.0 - findings["hedge_word_pct"], 4)

    db_path.unlink()


def test_analyze_style_metrics_values():
    """Test specific style metric values with known data."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE reply_pairs (
            id INTEGER PRIMARY KEY,
            inbound_text TEXT,
            reply_text TEXT,
            inbound_author TEXT,
            reply_author TEXT,
            metadata_json TEXT DEFAULT '{}'
        )
    """)
    # Reply with bullets, hedge word, question, and emoji
    conn.execute(
        "INSERT INTO reply_pairs (reply_text, inbound_author, reply_author) VALUES (?, ?, ?)",
        ("Maybe we should try:\n- option A\n- option B\n\nWhat do you think? 😊", "a@b.com", "me@x.com"),
    )
    # Reply without any of the above
    conn.execute(
        "INSERT INTO reply_pairs (reply_text, inbound_author, reply_author) VALUES (?, ?, ?)",
        ("Sounds good. Let's proceed with the plan.", "c@d.com", "me@x.com"),
    )
    conn.commit()
    conn.close()

    findings = analyze(db_path)
    assert findings["bullet_point_pct"] == 0.5  # 1 out of 2
    assert findings["hedge_word_pct"] == 0.5  # 1 out of 2 ('maybe')
    assert findings["directness_score"] == 0.5  # 1.0 - 0.5
    assert findings["emoji_pct"] == 0.5  # 1 out of 2
    assert findings["question_frequency"] > 0  # at least one question
    assert findings["avg_paragraphs"] >= 1

    db_path.unlink()


def test_assemble_prompt_style_constraints():
    """Test that style-driven constraints are injected into the prompt."""
    from app.generation.service import assemble_prompt

    persona = {
        "style": {
            "voice": "direct",
            "avg_reply_words": 40,
            "constraints": [],
            "bullet_point_pct": 0.5,
            "directness_score": 0.9,
            "avg_paragraphs": 3.0,
        }
    }
    prompt = assemble_prompt(
        inbound_message="test",
        reply_pairs=[],
        persona=persona,
        prompts={},
    )
    assert "prefer bullet points" in prompt
    assert "be direct, avoid hedging" in prompt
    assert "use clear paragraph breaks" in prompt


def test_assemble_prompt_no_style_constraints_when_low():
    """Test that style constraints are NOT added when metrics are low."""
    from app.generation.service import assemble_prompt

    persona = {
        "style": {
            "voice": "direct",
            "constraints": [],
            "bullet_point_pct": 0.1,
            "directness_score": 0.5,
            "avg_paragraphs": 1.5,
        }
    }
    prompt = assemble_prompt(
        inbound_message="test",
        reply_pairs=[],
        persona=persona,
        prompts={},
    )
    assert "prefer bullet points" not in prompt
    assert "be direct, avoid hedging" not in prompt
    assert "use clear paragraph breaks" not in prompt
