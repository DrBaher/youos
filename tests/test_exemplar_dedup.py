"""Tests for exemplar thread deduplication."""

from app.generation.service import _deduplicate_by_thread, _format_exemplars
from app.retrieval.service import RetrievalMatch


def _make_match(thread_id: str | None, score: float, reply: str = "test reply") -> RetrievalMatch:
    return RetrievalMatch(
        result_type="reply_pair",
        score=score,
        lexical_score=score,
        metadata_score=0.0,
        source_type="email",
        source_id="test",
        account_email=None,
        title=None,
        author=None,
        external_uri=None,
        thread_id=thread_id,
        created_at=None,
        updated_at=None,
        inbound_text="test inbound",
        reply_text=reply,
    )


def test_dedup_keeps_highest_score_per_thread():
    pairs = [
        _make_match("t1", 3.0, "low"),
        _make_match("t1", 8.0, "high"),
        _make_match("t2", 5.0, "only"),
    ]
    result = _deduplicate_by_thread(pairs)
    thread_ids = [r.thread_id for r in result]
    assert thread_ids.count("t1") == 1
    assert thread_ids.count("t2") == 1
    t1_match = [r for r in result if r.thread_id == "t1"][0]
    assert t1_match.score == 8.0


def test_dedup_none_thread_ids_treated_as_unique():
    pairs = [
        _make_match(None, 3.0, "a"),
        _make_match(None, 5.0, "b"),
        _make_match("t1", 4.0, "c"),
    ]
    result = _deduplicate_by_thread(pairs)
    assert len(result) == 3  # both None entries + t1


def test_dedup_empty():
    assert _deduplicate_by_thread([]) == []


def test_format_exemplars_deduplicates():
    """format_exemplars should deduplicate before formatting."""
    pairs = [
        _make_match("t1", 5.0, "reply A"),
        _make_match("t1", 3.0, "reply B"),
        _make_match("t2", 4.0, "reply C"),
    ]
    result = _format_exemplars(pairs)
    # Should only have 2 examples (one per thread)
    assert result.count("[EXAMPLE") == 2
    # Should keep the higher-scored reply for t1
    assert "reply A" in result
    assert "reply B" not in result
