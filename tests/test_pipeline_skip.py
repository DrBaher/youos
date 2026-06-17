"""Tests for smart pipeline skip gates (Item 7)."""

import sqlite3
from datetime import datetime, timedelta, timezone

from scripts.nightly_pipeline import (
    should_skip_autoresearch,
    should_skip_dedup,
    should_skip_embeddings,
    should_skip_finetune,
)


def _add_autoresearch_runs(db, runs):
    """runs: list of (run_tag, kept, created_ts) tuples."""
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS autoresearch_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "run_tag TEXT, iteration INTEGER, surface_name TEXT, mutation_desc TEXT, "
        "baseline_composite REAL, candidate_composite REAL, outcome TEXT, kept INTEGER, created_ts TEXT)"
    )
    for tag, kept, ts in runs:
        conn.execute(
            "INSERT INTO autoresearch_runs (run_tag, kept, created_ts, baseline_composite, candidate_composite) "
            "VALUES (?, ?, ?, 0.4, 0.4)",
            (tag, kept, ts),
        )
    conn.commit()
    conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _create_db(tmp_path, *, pairs=0, feedback=0, null_embeddings=0, reply_pair_null=0, reply_pair_embedded=0):
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE reply_pairs (
            id INTEGER PRIMARY KEY, inbound_text TEXT, reply_text TEXT,
            created_ts TEXT DEFAULT CURRENT_TIMESTAMP, auto_feedback_processed INTEGER DEFAULT 0,
            quality_score REAL DEFAULT 1.0
        )"""
    )
    conn.execute(
        """CREATE TABLE feedback_pairs (
            id INTEGER PRIMARY KEY, inbound_text TEXT, generated_draft TEXT,
            edited_reply TEXT, used_in_finetune INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        """CREATE TABLE chunks (
            id INTEGER PRIMARY KEY, document_id INTEGER, chunk_index INTEGER,
            content TEXT, embedding BLOB, metadata_json TEXT DEFAULT '{}'
        )"""
    )
    for i in range(pairs):
        conn.execute("INSERT INTO reply_pairs (inbound_text, reply_text) VALUES (?, ?)", (f"q{i}", f"a{i}"))
    for i in range(feedback):
        conn.execute(
            "INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply) VALUES (?, ?, ?)",
            (f"q{i}", f"d{i}", f"r{i}"),
        )
    for i in range(null_embeddings):
        conn.execute("INSERT INTO chunks (document_id, chunk_index, content, embedding) VALUES (?, ?, ?, ?)", (1, i, f"c{i}", None))
    # reply_pairs gets its embedding column lazily (mirrors the real migration:
    # the column is added by the indexer, not present in the base schema).
    if reply_pair_null or reply_pair_embedded:
        conn.execute("ALTER TABLE reply_pairs ADD COLUMN embedding BLOB")
        for i in range(reply_pair_null):
            conn.execute(
                "INSERT INTO reply_pairs (inbound_text, reply_text, embedding) VALUES (?, ?, ?)",
                (f"rn{i}", f"rn{i}", None),
            )
        for i in range(reply_pair_embedded):
            conn.execute(
                "INSERT INTO reply_pairs (inbound_text, reply_text, embedding) VALUES (?, ?, ?)",
                (f"re{i}", f"re{i}", b"\x00\x00\x00\x00"),
            )
    conn.commit()
    conn.close()
    return db


def test_skip_finetune_when_few_pairs(tmp_path):
    db = _create_db(tmp_path, feedback=2)
    skip, msg = should_skip_finetune(db)
    assert skip is True
    assert "only" in msg and "need >= 3" in msg


def test_no_skip_finetune_enough_pairs(tmp_path):
    db = _create_db(tmp_path, feedback=5)
    skip, _ = should_skip_finetune(db)
    assert skip is False  # 5 >= 3, no skip


def test_skip_autoresearch_when_few_pairs(tmp_path):
    db = _create_db(tmp_path, feedback=3)
    skip, msg = should_skip_autoresearch(db)
    assert skip is True
    assert "need >= 5" in msg


def test_no_skip_autoresearch_enough_pairs(tmp_path):
    db = _create_db(tmp_path, feedback=10)
    skip, _ = should_skip_autoresearch(db)
    assert skip is False


def test_skip_autoresearch_when_stalled(tmp_path):
    """b278: the last 3 sweeps kept 0 improvements -> inert, skip."""
    db = _create_db(tmp_path, feedback=10)
    _add_autoresearch_runs(db, [("r1", 0, _now()), ("r2", 0, _now()), ("r3", 0, _now())])
    skip, msg = should_skip_autoresearch(db)
    assert skip is True
    assert "stalled" in msg


def test_no_skip_autoresearch_when_recent_improvement(tmp_path):
    """A kept improvement in the recent window means the loop is live."""
    db = _create_db(tmp_path, feedback=10)
    _add_autoresearch_runs(db, [("r1", 1, _now()), ("r2", 0, _now()), ("r3", 0, _now())])
    skip, _ = should_skip_autoresearch(db)
    assert skip is False


def test_no_skip_autoresearch_insufficient_history(tmp_path):
    """Fewer than the required run history -> don't call it stalled yet."""
    db = _create_db(tmp_path, feedback=10)
    _add_autoresearch_runs(db, [("r1", 0, _now()), ("r2", 0, _now())])
    skip, _ = should_skip_autoresearch(db)
    assert skip is False


def test_autoresearch_rechecks_after_stale_period(tmp_path):
    """Even when stalled, force a periodic re-check so it can't stay off forever."""
    db = _create_db(tmp_path, feedback=10)
    old = (datetime.now(timezone.utc) - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S")
    _add_autoresearch_runs(db, [("r1", 0, old), ("r2", 0, old), ("r3", 0, old)])
    skip, _ = should_skip_autoresearch(db)
    assert skip is False  # > 14 days since the last sweep -> let it run again


def test_skip_embeddings_all_indexed(tmp_path):
    db = _create_db(tmp_path)
    skip, msg = should_skip_embeddings(db)
    assert skip is True
    assert "already indexed" in msg


def test_no_skip_embeddings_when_null(tmp_path):
    db = _create_db(tmp_path, null_embeddings=5)
    skip, _ = should_skip_embeddings(db)
    assert skip is False


def test_no_skip_embeddings_when_reply_pairs_null(tmp_path):
    # Regression: chunks fully embedded but reply_pairs has a backlog.
    # The old chunks-only gate skipped here, leaving the primary retrieval
    # table un-embedded forever.
    db = _create_db(tmp_path, null_embeddings=0, reply_pair_null=5)
    skip, _ = should_skip_embeddings(db)
    assert skip is False


def test_skip_embeddings_when_both_tables_indexed(tmp_path):
    # Both chunks and reply_pairs fully embedded → still skips.
    db = _create_db(tmp_path, reply_pair_embedded=5)
    skip, _ = should_skip_embeddings(db)
    assert skip is True


def test_no_skip_embeddings_when_reply_pairs_unmigrated(tmp_path):
    # reply_pairs exists with rows but no embedding column yet (first run
    # after upgrade) → must run so the indexer migrates and embeds.
    db = _create_db(tmp_path, pairs=5)
    skip, _ = should_skip_embeddings(db)
    assert skip is False


def test_skip_dedup_small_corpus(tmp_path):
    db = _create_db(tmp_path, pairs=5)
    skip, msg = should_skip_dedup(db)
    assert skip is True
    assert "too small" in msg


def test_no_skip_dedup_enough_pairs(tmp_path):
    db = _create_db(tmp_path, pairs=15)
    skip, _ = should_skip_dedup(db)
    assert skip is False


def test_skip_with_nonexistent_db(tmp_path):
    db = tmp_path / "nonexistent.db"
    skip, _ = should_skip_finetune(db)
    assert skip is True
    skip, _ = should_skip_dedup(db)
    assert skip is True
