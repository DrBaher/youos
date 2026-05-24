"""Regression: retrieval surfaces feedback quality_score into match metadata.

`_top_exemplar_source_ids` ranks exemplars by `metadata["quality_score"]` first
(see test_style_anchor_cache). That key is dead unless the retrieval scorer copies
the `reply_pairs.quality_score` column into the match metadata — this guards that
wiring on both the FTS and legacy scoring paths.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.retrieval.service import RetrievalConfig, RetrievalRequest, RetrievalService

_COLUMNS = [
    "id",
    "source_id",
    "source_type",
    "title",
    "author",
    "reply_author",
    "external_uri",
    "thread_id",
    "document_id",
    "created_at",
    "updated_at",
    "paired_at",
    "inbound_author",
    "inbound_text",
    "reply_text",
    "language",
    "metadata_json",
    "quality_score",
]


def _row(quality_score: float) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ", ".join(_COLUMNS)
    placeholders = ", ".join("?" for _ in _COLUMNS)
    conn.execute(f"CREATE TABLE rp ({cols})")
    conn.execute(
        f"INSERT INTO rp ({cols}) VALUES ({placeholders})",
        (
            1,
            "msg-1",
            "gmail",
            "Coffee?",
            "Alice",
            "Bob",
            None,
            "thread-1",
            10,
            "2026-01-01",
            "2026-01-01",
            "2026-01-01",
            "Alice",
            "want to grab coffee tomorrow",
            "sure, sounds good",
            "en",
            json.dumps({"subject": "Coffee?"}),
            quality_score,
        ),
    )
    return conn.execute("SELECT * FROM rp").fetchone()


def _service() -> RetrievalService:
    config = RetrievalConfig(
        top_k_documents=3,
        top_k_chunks=3,
        top_k_reply_pairs=5,
        recency_boost_days=90,
        recency_boost_weight=0.2,
        account_boost_weight=0.15,
        source_weights={},
    )
    return RetrievalService(db_path=Path(":memory:"), config=config)


def test_legacy_scorer_surfaces_quality_score():
    match = _service()._score_reply_pair_row_legacy(
        _row(0.7), query="coffee", tokens=["coffee"], request=RetrievalRequest(query="coffee")
    )
    assert match is not None
    assert match.metadata["quality_score"] == 0.7


def test_fts_scorer_surfaces_quality_score():
    row = _row(0.4)
    # FTS scorer reads an fts_rank column; reuse the same row via a wrapper dict.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ", ".join([*_COLUMNS, "fts_rank"])
    placeholders = ", ".join("?" for _ in range(len(_COLUMNS) + 1))
    conn.execute(f"CREATE TABLE rp ({cols})")
    conn.execute(
        f"INSERT INTO rp ({cols}) VALUES ({placeholders})",
        (*tuple(row), -2.5),
    )
    fts_row = conn.execute("SELECT * FROM rp").fetchone()
    match = _service()._score_reply_pair_row_fts(
        fts_row, query="coffee", tokens=["coffee"], request=RetrievalRequest(query="coffee")
    )
    assert match is not None
    assert match.metadata["quality_score"] == 0.4


def test_quality_score_scales_relevance():
    service = _service()
    req = RetrievalRequest(query="coffee")
    high = service._score_reply_pair_row_legacy(_row(1.2), query="coffee", tokens=["coffee"], request=req)
    low = service._score_reply_pair_row_legacy(_row(0.5), query="coffee", tokens=["coffee"], request=req)
    assert high.score > low.score
