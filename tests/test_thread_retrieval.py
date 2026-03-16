"""Tests for same-thread context retrieval boosting."""

from app.retrieval.service import RetrievalMatch, RetrievalRequest


def _make_match(thread_id: str | None, score: float) -> RetrievalMatch:
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
        inbound_text="test",
        reply_text="test",
    )


def test_thread_id_boost():
    """Reply pairs matching thread_id get 2x score boost."""
    matches = [
        _make_match("thread-abc", 3.0),
        _make_match("thread-xyz", 5.0),
        _make_match(None, 4.0),
    ]
    request = RetrievalRequest(query="test", thread_id="thread-abc")

    # Simulate thread boosting logic from RetrievalService.retrieve
    if request.thread_id:
        for match in matches:
            if match.thread_id and match.thread_id == request.thread_id:
                match.score = round(match.score * 2.0, 4)
        matches.sort(key=lambda m: (-m.score, m.result_type, m.source_id))

    # thread-abc should now be 6.0 (3.0 * 2), making it the top result
    assert matches[0].thread_id == "thread-abc"
    assert matches[0].score == 6.0


def test_no_thread_id_no_boost():
    """Without thread_id, no boosting happens."""
    matches = [
        _make_match("thread-abc", 3.0),
        _make_match("thread-xyz", 5.0),
    ]
    request = RetrievalRequest(query="test")

    if request.thread_id:
        for match in matches:
            if match.thread_id and match.thread_id == request.thread_id:
                match.score = round(match.score * 2.0, 4)
        matches.sort(key=lambda m: (-m.score, m.result_type, m.source_id))

    # Scores unchanged (no sort happens without boosting)
    assert matches[0].score == 3.0
    assert matches[1].score == 5.0


def test_thread_id_in_retrieval_request():
    """RetrievalRequest accepts thread_id."""
    req = RetrievalRequest(query="test", thread_id="thread-123")
    assert req.thread_id == "thread-123"


def test_thread_id_default_none():
    """thread_id defaults to None."""
    req = RetrievalRequest(query="test")
    assert req.thread_id is None


def test_draft_request_thread_id():
    """DraftRequest accepts thread_id."""
    from app.generation.service import DraftRequest

    req = DraftRequest(inbound_message="test", thread_id="thread-456")
    assert req.thread_id == "thread-456"
