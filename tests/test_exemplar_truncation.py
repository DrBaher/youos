"""Tests for exemplar truncation priorities (Item 5)."""

from __future__ import annotations

from app.generation.service import _format_exemplars
from app.retrieval.service import RetrievalMatch


def _make_match(inbound: str, reply: str, score: float = 5.0) -> RetrievalMatch:
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
        inbound_text=inbound,
        reply_text=reply,
    )


def test_reply_cap_is_600():
    """Reply text should be capped at 600 chars."""
    long_reply = "x" * 1000
    match = _make_match(inbound="short", reply=long_reply)
    text = _format_exemplars([match])
    # The reply in the formatted text should be at most 600 chars
    # Find "Your reply: " and check length
    assert "x" * 600 in text
    assert "x" * 601 not in text


def test_inbound_cap_is_400():
    """Inbound text should be capped at 400 chars."""
    long_inbound = "y" * 1000
    match = _make_match(inbound=long_inbound, reply="short reply")
    text = _format_exemplars([match])
    assert "y" * 400 in text
    assert "y" * 401 not in text


def test_reply_gets_more_space_than_inbound():
    """Reply cap (600) > inbound cap (400)."""
    long_text = "z" * 1000
    match = _make_match(inbound=long_text, reply=long_text)
    text = _format_exemplars([match])
    # Find the inbound and reply sections
    inbound_start = text.index("Inbound: ")
    reply_start = text.index("Your reply: ")
    inbound_section = text[inbound_start:reply_start]
    reply_section = text[reply_start:]
    # Reply section should be longer than inbound section
    assert len(reply_section) > len(inbound_section)
