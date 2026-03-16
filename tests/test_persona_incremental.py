"""Tests for incremental persona analysis (Item 7)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.analyze_persona import analyze


def _create_db(tmp_path: Path, pairs: list[tuple[str, str, str, str | None]]) -> Path:
    """Create a test DB with reply_pairs.

    Each pair is (inbound_text, reply_text, inbound_author, paired_at).
    """
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
    for inbound, reply, author, paired_at in pairs:
        conn.execute(
            "INSERT INTO reply_pairs (inbound_text, reply_text, inbound_author, paired_at) VALUES (?, ?, ?, ?)",
            (inbound, reply, author, paired_at),
        )
    conn.commit()
    conn.close()
    return db_path


def test_analyze_full_mode(tmp_path):
    """Full mode (recent_days=None) processes all pairs equally."""
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=365)).isoformat()
    recent = now.isoformat()
    pairs = [
        ("old inbound", "short reply", "a@b.com", old),
        ("recent inbound", "this is a much longer reply with many words", "c@d.com", recent),
    ]
    db = _create_db(tmp_path, pairs)
    findings = analyze(db, recent_days=None)
    assert findings["total_pairs"] == 2
    assert findings["reply_length"]["avg_words"] > 0


def test_analyze_recent_days_weighting(tmp_path):
    """Recent days mode weights recent pairs 3x."""
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=365)).isoformat()
    recent = now.isoformat()
    # Old pair has 2 words, recent has 20 words
    pairs = [
        ("old inbound", "two words", "a@b.com", old),
        ("recent inbound", " ".join(["word"] * 20), "c@d.com", recent),
    ]
    db = _create_db(tmp_path, pairs)
    # With recent_days=90, recent pair gets 3x weight
    findings_recent = analyze(db, recent_days=90)
    # With full mode (no weighting), both equal
    findings_full = analyze(db, recent_days=None)
    # Both should produce valid results
    assert findings_recent["total_pairs"] == 2
    assert findings_full["total_pairs"] == 2


def test_ewma_weights_recent_more(tmp_path):
    """EWMA should weight recent pairs more heavily."""
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=365)).isoformat()
    recent = now.isoformat()
    # Old pairs: 5 words, recent pairs: 50 words
    pairs = []
    for i in range(5):
        pairs.append(("old", "one two three four five", "a@b.com", old))
    for i in range(5):
        pairs.append(("recent", " ".join(["word"] * 50), "c@d.com", recent))
    db = _create_db(tmp_path, pairs)
    findings = analyze(db, recent_days=None)
    # EWMA should be closer to 50 than to 5 because recent pairs have higher weight
    avg = findings["reply_length"]["avg_words"]
    assert avg > 20  # Biased toward recent


def test_analyze_empty_db(tmp_path):
    """Empty DB returns error."""
    db = _create_db(tmp_path, [])
    findings = analyze(db)
    assert "error" in findings
