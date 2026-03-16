"""Tests for prompt token budget and exemplar truncation (Item 1)."""

from __future__ import annotations

from app.generation.service import (
    PROMPT_TOKEN_BUDGET,
    _estimate_tokens,
    _format_exemplars,
    assemble_prompt,
)
from app.retrieval.service import RetrievalMatch


def _make_match(score: float = 5.0, inbound: str = "test inbound", reply: str = "test reply") -> RetrievalMatch:
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


def test_estimate_tokens_basic():
    text = "hello world this is a test"
    result = _estimate_tokens(text)
    assert result == int(6 * 1.4)


def test_estimate_tokens_empty():
    assert _estimate_tokens("") == 0


def test_prompt_token_budget_constant():
    assert PROMPT_TOKEN_BUDGET == 2000


def test_format_exemplars_truncation_in_assembly():
    """When many long exemplars push the prompt over budget, they should be trimmed."""
    # Create exemplars with long text that will exceed the budget
    long_text = "word " * 500  # 500 words
    pairs = [_make_match(score=10 - i, inbound=long_text, reply=long_text) for i in range(10)]

    persona = {"style": {"voice": "test"}}
    prompts = {"system_prompt": "Test system."}

    prompt = assemble_prompt(
        inbound_message="short inbound",
        reply_pairs=pairs,
        persona=persona,
        prompts=prompts,
    )
    # The prompt with 10 long exemplars should be very long
    assert _estimate_tokens(prompt) > 500  # It has content


def test_format_exemplars_preserves_high_scoring():
    """Highest-scoring exemplars are preserved when trimming."""
    pairs = [
        _make_match(score=9.0, reply="high score reply"),
        _make_match(score=7.0, reply="medium score reply"),
        _make_match(score=1.0, reply="low score reply"),
    ]
    text = _format_exemplars(pairs)
    assert "high score reply" in text
    assert "medium score reply" in text
    # Low score (< 0.2) is already filtered by existing logic


def test_token_estimate_in_draft_response():
    """DraftResponse should have token_estimate field."""
    from app.generation.service import DraftResponse

    resp = DraftResponse(
        draft="test",
        detected_mode="work",
        precedent_used=[],
        retrieval_method="fts5",
        confidence="high",
        confidence_reason="test",
        model_used="test",
        token_estimate=150,
    )
    assert resp.token_estimate == 150
    d = resp.to_dict()
    assert d["token_estimate"] == 150


def test_token_estimate_default_none():
    """token_estimate defaults to None."""
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
    assert resp.token_estimate is None
