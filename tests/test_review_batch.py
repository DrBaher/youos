"""Tests for configurable review batch size."""

import sqlite3
from datetime import datetime, timedelta, timezone

from app.api.review_queue_routes import _fetch_candidates
from app.core.config import get_review_batch_size


def test_default_batch_size():
    assert get_review_batch_size({}) == 10


def test_custom_batch_size():
    assert get_review_batch_size({"review": {"batch_size": 20}}) == 20


def test_batch_size_clamp_min():
    assert get_review_batch_size({"review": {"batch_size": 1}}) == 5


def test_batch_size_clamp_max():
    assert get_review_batch_size({"review": {"batch_size": 100}}) == 50


def test_batch_size_missing_section():
    assert get_review_batch_size({"user": {"name": "Test"}}) == 10


def _seed_review_db(tmp_path):
    """A pool of 3 higher-base external_client pairs + 1 lower-base personal pair.

    Base score = recency + length; the 3 external pairs win on length (100–500
    chars). With batch_size=3, a selector that ignores diversity picks all three
    external_client pairs; a working one swaps the 3rd for the personal pair once
    external_client saturates (b198)."""
    db = tmp_path / "rq.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, title TEXT, metadata_json TEXT)")
    conn.execute("CREATE TABLE feedback_pairs (id INTEGER PRIMARY KEY, reply_pair_id INTEGER)")
    conn.execute(
        "CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY, inbound_text TEXT, inbound_author TEXT, "
        "reply_text TEXT, paired_at TEXT, metadata_json TEXT, document_id INTEGER)"
    )
    recent = (datetime.now(tz=timezone.utc) - timedelta(days=3)).isoformat()
    long_body = "Hi, following up on the proposal from last week — could you share your thoughts when you get a chance? " + "x" * 60
    short_body = "Hey, are we still on for dinner this Saturday evening?"  # >=50, <100 → no length bonus
    rows = [
        (1, long_body, "alice@acmecorp.com", "Sure, sounds good to me.", recent),
        (2, long_body, "bob@acmecorp.com", "Sure, sounds good to me.", recent),
        (3, long_body, "carol@acmecorp.com", "Sure, sounds good to me.", recent),
        (4, short_body, "dave@gmail.com", "Yes, absolutely — see you then!", recent),  # personal, lower base
    ]
    conn.executemany(
        "INSERT INTO reply_pairs (id, inbound_text, inbound_author, reply_text, paired_at, metadata_json, document_id) "
        "VALUES (?, ?, ?, ?, ?, '{}', NULL)",
        rows,
    )
    conn.commit()
    conn.close()
    return db


def test_batch_selection_is_diversity_aware(tmp_path):
    """The diversity bonus actually varies the batch: the lower-base personal
    pair is selected over a 3rd external_client pair once that type saturates.
    Previously the whole pool was scored against an empty Counter, so the bonus
    was a constant and the batch was all external_client (b198)."""
    db = _seed_review_db(tmp_path)
    candidates, _total = _fetch_candidates(db, batch_size=3, exclude_ids=[])
    authors = {c["inbound_author"] for c in candidates}
    assert len(candidates) == 3
    # The personal (gmail) pair must make the cut despite its lower base score.
    assert "dave@gmail.com" in authors
