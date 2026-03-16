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

    db_path.unlink()
