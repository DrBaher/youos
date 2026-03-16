"""Tests for relative confidence thresholds (Item 3)."""

from __future__ import annotations

from app.generation.service import _score_confidence
from app.retrieval.service import RetrievalMatch


def _make_match(score: float) -> RetrievalMatch:
    return RetrievalMatch(
        result_type="reply_pair",
        score=score,
        lexical_score=0.5,
        metadata_score=0.5,
        source_type="email",
        source_id="test",
        account_email=None,
        title=None,
        author=None,
        external_uri=None,
        thread_id=None,
        created_at=None,
        updated_at=None,
    )


def test_relative_high_confidence():
    """Top score > mean + stddev → high."""
    pairs = [_make_match(10.0), _make_match(3.0), _make_match(2.0)]
    stats = {"mean": 5.0, "stddev": 2.0, "max": 10.0}
    conf, reason = _score_confidence(pairs, score_stats=stats)
    assert conf == "high"
    assert "mean+1σ" in reason


def test_relative_medium_confidence():
    """Top score > mean but < mean + stddev → medium."""
    pairs = [_make_match(6.0), _make_match(4.0)]
    stats = {"mean": 5.0, "stddev": 3.0, "max": 6.0}
    conf, reason = _score_confidence(pairs, score_stats=stats)
    assert conf == "medium"
    assert "above mean" in reason


def test_relative_low_confidence():
    """Top score < mean → low."""
    pairs = [_make_match(3.0), _make_match(2.0)]
    stats = {"mean": 5.0, "stddev": 1.0, "max": 3.0}
    conf, reason = _score_confidence(pairs, score_stats=stats)
    assert conf == "low"
    assert "below mean" in reason


def test_absolute_fallback_no_stats():
    """When no stats provided, fall back to absolute thresholds."""
    pairs = [_make_match(9.0), _make_match(9.0), _make_match(9.0)]
    conf, _ = _score_confidence(pairs, score_stats=None)
    assert conf == "high"


def test_absolute_fallback_empty_pairs():
    """Empty pairs → low."""
    conf, _ = _score_confidence([], score_stats=None)
    assert conf == "low"


def test_retrieval_response_has_score_stats():
    """RetrievalResponse should have max_score, mean_score, score_stddev fields."""
    from app.retrieval.service import RetrievalResponse

    resp = RetrievalResponse(
        query="test",
        retrieval_method="fts5",
        semantic_search_enabled=False,
        applied_filters={},
        detected_mode="work",
        documents=[],
        chunks=[],
        reply_pairs=[],
        max_score=8.0,
        mean_score=5.0,
        score_stddev=2.0,
    )
    assert resp.max_score == 8.0
    assert resp.mean_score == 5.0
    assert resp.score_stddev == 2.0


def test_retrieval_response_defaults_none():
    """Score stats default to None."""
    from app.retrieval.service import RetrievalResponse

    resp = RetrievalResponse(
        query="test",
        retrieval_method="fts5",
        semantic_search_enabled=False,
        applied_filters={},
        detected_mode="work",
        documents=[],
        chunks=[],
        reply_pairs=[],
    )
    assert resp.max_score is None
    assert resp.mean_score is None
    assert resp.score_stddev is None
