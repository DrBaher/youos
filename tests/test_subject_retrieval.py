"""Tests for subject line as retrieval signal (Item 6)."""

from __future__ import annotations

from app.retrieval.service import RetrievalConfig, RetrievalMatch, _field_match_bonus, _tokenize


def test_subject_match_boost_config_default():
    """RetrievalConfig has subject_match_boost default of 0.2."""
    config = RetrievalConfig(
        top_k_documents=3,
        top_k_chunks=3,
        top_k_reply_pairs=5,
        recency_boost_days=90,
        recency_boost_weight=0.2,
        account_boost_weight=0.15,
        source_weights={},
    )
    assert config.subject_match_boost == 0.2


def test_retrieval_match_has_subject_field():
    """RetrievalMatch has a subject field."""
    match = RetrievalMatch(
        result_type="reply_pair",
        score=5.0,
        lexical_score=4.0,
        metadata_score=1.0,
        source_type="gmail",
        source_id="msg-1",
        account_email=None,
        title=None,
        author=None,
        external_uri=None,
        thread_id=None,
        created_at=None,
        updated_at=None,
        subject="Q2 Roadmap Discussion",
    )
    assert match.subject == "Q2 Roadmap Discussion"


def test_field_match_bonus_with_subject():
    """Subject text contributes to field match bonus."""
    tokens = _tokenize("roadmap discussion q2")
    bonus = _field_match_bonus("Q2 Roadmap Discussion", tokens)
    assert bonus > 0


def test_subject_in_retrieval_match_dict():
    """Subject is included in to_dict output."""
    match = RetrievalMatch(
        result_type="reply_pair",
        score=5.0,
        lexical_score=4.0,
        metadata_score=1.0,
        source_type="gmail",
        source_id="msg-1",
        account_email=None,
        title=None,
        author=None,
        external_uri=None,
        thread_id=None,
        created_at=None,
        updated_at=None,
        subject="Test Subject",
    )
    d = match.to_dict()
    assert d["subject"] == "Test Subject"
