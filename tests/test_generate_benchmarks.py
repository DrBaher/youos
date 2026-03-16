"""Tests for benchmark generation."""

import json
import sqlite3
from pathlib import Path

import yaml

from scripts.generate_benchmarks import (
    _classify_sender_type,
    _extract_keywords,
    _make_case_key,
    generate_cases,
    write_fixtures,
)


def test_classify_sender_personal():
    assert _classify_sender_type("alice@gmail.com", "{}") == "personal"


def test_classify_sender_external():
    assert _classify_sender_type("john@company.com", "{}") == "external_client"


def test_classify_sender_from_metadata():
    meta = json.dumps({"sender_type": "internal"})
    assert _classify_sender_type("alice@company.com", meta) == "internal"


def test_classify_sender_unknown():
    assert _classify_sender_type("", "{}") == "unknown"
    assert _classify_sender_type(None, None) == "unknown"


def test_extract_keywords():
    text = "Please review the deployment pipeline configuration and update the documentation"
    kw = _extract_keywords(text, max_keywords=3)
    assert isinstance(kw, list)
    assert len(kw) <= 3
    assert all(isinstance(w, str) for w in kw)


def test_extract_keywords_short_text():
    kw = _extract_keywords("ok yes", max_keywords=3)
    assert isinstance(kw, list)


def test_make_case_key_from_subject():
    key = _make_case_key("Re: Q4 Budget Review", "some text")
    assert "q4" in key.lower()
    assert "budget" in key.lower()


def test_make_case_key_from_text():
    key = _make_case_key(None, "Can we schedule a meeting next week?")
    assert len(key) > 0
    assert len(key) <= 50


def _create_test_db(tmp_path: Path) -> Path:
    """Create a test database with sample reply pairs."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE reply_pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL DEFAULT 'gmail',
            source_id TEXT NOT NULL,
            inbound_text TEXT NOT NULL,
            reply_text TEXT NOT NULL,
            inbound_author TEXT,
            reply_author TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE TABLE benchmark_cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_key TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL,
            prompt_text TEXT NOT NULL,
            expected_properties_json TEXT NOT NULL DEFAULT '{}'
        )
    """)

    # Insert test data - enough for benchmarks
    pairs = [
        (
            "src_1",
            "Can you review the Q4 budget proposal?",
            "Sure, I'll take a look at the numbers and get back to you by Friday.",
            "john@company.com",
            '{"subject": "Q4 Budget"}',
        ),
        ("src_2", "Hey, are we still on for Saturday?", "Yes! Looking forward to it. Let's meet at noon.", "friend@gmail.com", "{}"),
        (
            "src_3",
            "Please confirm the delivery schedule for next month",
            "The delivery is scheduled for March 15. I've attached the updated timeline.",
            "supplier@logistics.com",
            "{}",
        ),
        (
            "src_4",
            "Following up on the partnership proposal we discussed",
            "Thanks for following up. We're interested in moving forward with the integration.",
            "partner@techco.com",
            '{"sender_type": "external_client"}',
        ),
        (
            "src_5",
            "Quick question about the API rate limits",
            "The current limit is 1000 requests per minute. Let me know if you need it increased.",
            "dev@startup.io",
            "{}",
        ),
    ]
    for src_id, inbound, reply, author, meta in pairs:
        conn.execute(
            "INSERT INTO reply_pairs (source_type, source_id, inbound_text, reply_text, inbound_author, metadata_json) VALUES ('gmail', ?, ?, ?, ?, ?)",
            (src_id, inbound, reply, author, meta),
        )
    conn.commit()
    conn.close()
    return db_path


def test_generate_cases(tmp_path):
    db_path = _create_test_db(tmp_path)
    cases = generate_cases(db_path, count=5)
    assert len(cases) > 0
    assert len(cases) <= 5

    for case in cases:
        assert "case_key" in case
        assert "category" in case
        assert "prompt_text" in case
        assert "expected_properties" in case
        props = case["expected_properties"]
        assert "should_contain_keywords" in props
        assert "mode" in props
        assert "max_words" in props
        assert props["max_words"] >= 20


def test_generate_cases_empty_db(tmp_path):
    db_path = tmp_path / "empty.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE reply_pairs (
            id INTEGER PRIMARY KEY, source_type TEXT, source_id TEXT,
            inbound_text TEXT, reply_text TEXT, inbound_author TEXT, metadata_json TEXT
        )
    """)
    conn.commit()
    conn.close()
    cases = generate_cases(db_path, count=5)
    assert cases == []


def test_write_fixtures(tmp_path):
    cases = [
        {
            "case_key": "test_case",
            "category": "external_client",
            "prompt_text": "Hello, can you help?",
            "expected_properties": {
                "should_contain_keywords": ["help"],
                "mode": "work",
                "max_words": 50,
            },
        }
    ]
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    path = fixtures_dir / "benchmark_cases.yaml"

    # Monkeypatch the module constants
    import scripts.generate_benchmarks as gb

    orig_dir = gb.FIXTURES_DIR
    orig_file = gb.BENCHMARK_FILE
    gb.FIXTURES_DIR = fixtures_dir
    gb.BENCHMARK_FILE = path
    try:
        result_path = write_fixtures(cases)
        assert result_path.exists()
        loaded = yaml.safe_load(result_path.read_text())
        assert len(loaded) == 1
        assert loaded[0]["case_key"] == "test_case"
    finally:
        gb.FIXTURES_DIR = orig_dir
        gb.BENCHMARK_FILE = orig_file


def test_unique_case_keys(tmp_path):
    """All generated case keys should be unique."""
    db_path = _create_test_db(tmp_path)
    cases = generate_cases(db_path, count=5)
    keys = [c["case_key"] for c in cases]
    assert len(keys) == len(set(keys)), f"Duplicate keys found: {keys}"
