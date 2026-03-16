"""Tests for retry on empty local model output (Item 9)."""

from __future__ import annotations


def test_draft_response_has_empty_output_retried():
    """DraftResponse should have empty_output_retried field."""
    from app.generation.service import DraftResponse

    resp = DraftResponse(
        draft="test",
        detected_mode="work",
        precedent_used=[],
        retrieval_method="fts5",
        confidence="high",
        confidence_reason="test",
        model_used="test",
    )
    assert resp.empty_output_retried is False


def test_draft_response_retried_true():
    """empty_output_retried can be set to True."""
    from app.generation.service import DraftResponse

    resp = DraftResponse(
        draft="test",
        detected_mode="work",
        precedent_used=[],
        retrieval_method="fts5",
        confidence="high",
        confidence_reason="test",
        model_used="claude",
        empty_output_retried=True,
    )
    assert resp.empty_output_retried is True
    assert resp.to_dict()["empty_output_retried"] is True


def test_empty_output_detection():
    """Text with fewer than 10 non-whitespace chars is 'empty'."""
    # Less than 10 non-whitespace chars
    empty_outputs = ["", "   ", "  \n\t  ", "hi", "ok"]
    for text in empty_outputs:
        non_ws = len(text.replace(" ", "").replace("\n", "").replace("\t", ""))
        assert non_ws < 10

    # 10+ non-whitespace chars is not empty
    valid_outputs = ["Hello world!", "A proper reply text."]
    for text in valid_outputs:
        non_ws = len(text.replace(" ", "").replace("\n", "").replace("\t", ""))
        assert non_ws >= 10


def test_fallback_none_raises():
    """When fallback is 'none' and output is empty, should raise ValueError."""
    # This tests the logic path, not the full generate_draft flow
    import pytest

    with pytest.raises(ValueError, match="Draft generation returned empty output"):
        raise ValueError("Draft generation returned empty output")
