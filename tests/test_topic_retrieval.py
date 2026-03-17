"""Tests for topic-aware retrieval boosting (Item 12)."""

from app.retrieval.service import (
    RetrievalConfig,
    _check_topic_overlap,
    _load_sender_topics,
    _tokenize,
)


def test_check_topic_overlap_match():
    tokens = _tokenize("Let's discuss the project timeline")
    topics = ["project", "timeline", "budget"]
    assert _check_topic_overlap(tokens, topics)


def test_check_topic_overlap_no_match():
    tokens = _tokenize("Hello there")
    topics = ["project", "timeline"]
    assert not _check_topic_overlap(tokens, topics)


def test_check_topic_overlap_short_tokens_ignored():
    """Tokens <=2 chars should not match (threshold is >2)."""
    tokens = ["at", "to"]
    topics = ["at"]
    assert not _check_topic_overlap(tokens, topics)


def test_check_topic_overlap_case_insensitive():
    tokens = _tokenize("We need to discuss the Budget")
    topics = ["budget", "forecast"]
    assert _check_topic_overlap(tokens, topics)


def test_load_sender_topics_no_table(tmp_path):
    import sqlite3

    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    topics = _load_sender_topics(conn, "example.com")
    assert topics == []
    conn.close()


def test_load_sender_topics_with_data(tmp_path):
    import json
    import sqlite3

    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE sender_profiles (
            id INTEGER PRIMARY KEY, email TEXT, display_name TEXT,
            domain TEXT, company TEXT, sender_type TEXT,
            relationship_note TEXT, reply_count INTEGER,
            avg_reply_words REAL, avg_response_hours REAL,
            first_seen TEXT, last_seen TEXT, topics_json TEXT,
            updated_at TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO sender_profiles (email, domain, topics_json) VALUES (?, ?, ?)",
        ("alice@acme.com", "acme.com", json.dumps(["project", "budget", "timeline"])),
    )
    conn.commit()
    conn.row_factory = sqlite3.Row
    topics = _load_sender_topics(conn, "acme.com")
    assert topics == ["project", "budget", "timeline"]
    conn.close()


def test_topic_match_boost_in_config():
    config = RetrievalConfig(
        top_k_documents=3,
        top_k_chunks=3,
        top_k_reply_pairs=8,
        recency_boost_days=60,
        recency_boost_weight=0.2,
        account_boost_weight=0.15,
        source_weights={},
    )
    assert config.topic_match_boost == 0.15


def test_topic_match_boost_defaults_yaml():
    from pathlib import Path

    import yaml

    defaults_path = Path(__file__).resolve().parents[1] / "configs" / "retrieval" / "defaults.yaml"
    data = yaml.safe_load(defaults_path.read_text())
    assert "topic_match_boost" in data
    assert data["topic_match_boost"] == 0.15
