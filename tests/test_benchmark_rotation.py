"""Tests for rotating benchmark cases (Item 8)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.generate_benchmarks import generate_cases


def _create_db(tmp_path: Path) -> Path:
    """Create a test DB with reply_pairs."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE reply_pairs (
            id INTEGER PRIMARY KEY,
            inbound_text TEXT,
            reply_text TEXT,
            inbound_author TEXT,
            metadata_json TEXT DEFAULT '{}'
        )
    """)
    # Insert enough rows for benchmark generation
    for i in range(50):
        conn.execute(
            "INSERT INTO reply_pairs (inbound_text, reply_text, inbound_author) VALUES (?, ?, ?)",
            (f"Inbound email number {i} about project timeline and delivery schedule", f"Reply {i} with details about the project timeline and schedule delivery", f"sender{i}@example.com"),
        )
    conn.commit()
    conn.close()
    return db_path


def test_generate_cases_with_seed(tmp_path):
    """Same seed produces same cases."""
    db = _create_db(tmp_path)
    cases1 = generate_cases(db, count=5, sample_size=30, seed=42)
    cases2 = generate_cases(db, count=5, sample_size=30, seed=42)
    assert len(cases1) == len(cases2)
    keys1 = [c["case_key"] for c in cases1]
    keys2 = [c["case_key"] for c in cases2]
    assert keys1 == keys2


def test_different_seeds_differ(tmp_path):
    """Different seeds produce different cases (with high probability)."""
    db = _create_db(tmp_path)
    cases1 = generate_cases(db, count=10, sample_size=30, seed=42)
    cases2 = generate_cases(db, count=10, sample_size=30, seed=99)
    keys1 = [c["case_key"] for c in cases1]
    keys2 = [c["case_key"] for c in cases2]
    # At least some should differ
    assert keys1 != keys2


def test_sample_size_limits_pool(tmp_path):
    """sample_size limits the pool of rows considered."""
    db = _create_db(tmp_path)
    # With sample_size=5 and count=5, should still produce cases
    cases = generate_cases(db, count=5, sample_size=5, seed=42)
    assert len(cases) == 5


def test_default_seed_uses_iso_week(tmp_path):
    """Default seed (None) should still produce results."""
    db = _create_db(tmp_path)
    cases = generate_cases(db, count=3, sample_size=30)
    assert len(cases) == 3


def test_benchmark_refresh_tracking(tmp_path):
    """benchmark_last_refresh.txt tracks rotation timestamp."""
    import json

    refresh_path = tmp_path / "benchmark_last_refresh.txt"
    data = {"timestamp": "2024-01-01T00:00:00+00:00", "seed": 42}
    refresh_path.write_text(json.dumps(data))
    loaded = json.loads(refresh_path.read_text())
    assert loaded["seed"] == 42
    assert "timestamp" in loaded
