"""Tests for generation improvements (Items 8, 10)."""

from __future__ import annotations

from unittest.mock import patch

from app.retrieval.service import RetrievalMatch


def _make_reply_match(inbound: str, reply: str, score: float = 5.0) -> RetrievalMatch:
    return RetrievalMatch(
        result_type="reply_pair", score=score, lexical_score=score,
        metadata_score=0.0, source_type="gmail", source_id="1",
        account_email=None, title=None, author=None, external_uri=None,
        thread_id=None, created_at=None, updated_at=None,
        inbound_text=inbound, reply_text=reply, snippet=inbound[:50],
    )


# --- Item 8: Few-shot formatted exemplars ---


def test_format_exemplars_few_shot_format():
    """Exemplars use [EXAMPLE N] format with header."""
    from app.generation.service import _format_exemplars

    matches = [
        _make_reply_match("Can we meet Tuesday?", "Sure, 2pm works."),
        _make_reply_match("Attached is the proposal.", "Thanks, will review."),
    ]
    result = _format_exemplars(matches)
    assert "The following are examples of how you have replied to similar emails:" in result
    assert "[EXAMPLE 1]" in result
    assert "[EXAMPLE 2]" in result
    assert "Inbound: Can we meet Tuesday?" in result
    assert "Your reply: Sure, 2pm works." in result
    assert "---" in result


def test_format_exemplars_max_5():
    """Exemplars are capped at 5."""
    from app.generation.service import _format_exemplars

    matches = [_make_reply_match(f"inbound {i}", f"reply {i}") for i in range(10)]
    result = _format_exemplars(matches)
    assert "[EXAMPLE 5]" in result
    assert "[EXAMPLE 6]" not in result


def test_format_exemplars_empty():
    """Empty matches returns no-exemplars message."""
    from app.generation.service import _format_exemplars

    assert _format_exemplars([]) == "(no exemplars found)"


def test_assemble_prompt_includes_few_shot():
    """assemble_prompt uses few-shot exemplar format."""
    from app.generation.service import assemble_prompt

    matches = [_make_reply_match("Question?", "Answer.")]
    with patch("app.generation.service.get_user_name", return_value="Test"):
        prompt = assemble_prompt(
            inbound_message="New question?",
            reply_pairs=matches,
            persona={"style": {"voice": "direct"}},
            prompts={"system_prompt": "You are YouOS."},
        )
    assert "[EXAMPLE 1]" in prompt
    assert "Your reply:" in prompt
    assert "The following are examples" in prompt
