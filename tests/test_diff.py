"""Tests for diff module — token_similarity and hybrid_similarity."""

from app.core.diff import (
    hybrid_similarity,
    is_meaningfully_different,
    similarity_ratio,
    token_similarity,
)

# --- token_similarity ---


def test_token_similarity_identical():
    assert token_similarity("hello world", "hello world") == 1.0


def test_token_similarity_empty_both():
    assert token_similarity("", "") == 1.0


def test_token_similarity_one_empty():
    assert token_similarity("hello", "") == 0.0
    assert token_similarity("", "world") == 0.0


def test_token_similarity_no_overlap():
    assert token_similarity("hello world", "foo bar") == 0.0


def test_token_similarity_partial_overlap():
    sim = token_similarity("hello world foo", "hello bar foo")
    # intersection: {hello, foo} = 2, union: {hello, world, foo, bar} = 4
    assert sim == 0.5


def test_token_similarity_case_insensitive():
    assert token_similarity("Hello World", "hello world") == 1.0


# --- hybrid_similarity ---


def test_hybrid_similarity_identical():
    assert hybrid_similarity("same text", "same text") == 1.0


def test_hybrid_similarity_empty():
    assert hybrid_similarity("", "") == 1.0


def test_hybrid_similarity_different():
    sim = hybrid_similarity("hello world", "goodbye universe")
    assert 0 <= sim <= 1


def test_hybrid_is_blend():
    """hybrid_similarity should be between sequence and token similarity."""
    a = "the quick brown fox"
    b = "the slow brown fox"
    seq = similarity_ratio(a, b)
    tok = token_similarity(a, b)
    hyb = hybrid_similarity(a, b)
    assert abs(hyb - (0.5 * seq + 0.5 * tok)) < 1e-9


# --- is_meaningfully_different uses hybrid ---


def test_is_meaningfully_different_identical():
    assert is_meaningfully_different("same", "same") is False


def test_is_meaningfully_different_completely_different():
    assert is_meaningfully_different("draft A", "actual reply B") is True


def test_is_meaningfully_different_with_reordered_words():
    """Reordering words should still be detected via hybrid similarity."""
    draft = "Please review the attached proposal"
    actual = "the proposal attached please review"
    # Token similarity is high (same words), sequence similarity is low
    # hybrid should be somewhere in between
    is_meaningfully_different(draft, actual, threshold=0.80)
    # Words are the same, so hybrid sim should be relatively high
    hyb = hybrid_similarity(draft, actual)
    assert hyb > 0.3  # not completely different


def test_backward_compat_similarity_ratio():
    """similarity_ratio should still work as before."""
    assert similarity_ratio("hello", "hello") == 1.0
    assert similarity_ratio("", "") == 1.0
    assert similarity_ratio("hello", "") == 0.0


def test_similarity_ratio_is_length_bounded_against_quadratic_dos():
    """b149: SequenceMatcher.ratio() is O(n*m) on adversarial input (~50s at
    n=40000); the inputs are attacker-controlled on the CSRF-able submit routes,
    so the sink bounds the length. Normal-length results are unchanged."""
    import time

    from app.core.diff import similarity_ratio

    a = "".join(chr(33 + (i % 90)) for i in range(1_000_000))
    t0 = time.perf_counter()
    similarity_ratio(a, a[::-1])
    assert time.perf_counter() - t0 < 1.0  # bounded, not ~minutes
    assert similarity_ratio("same text", "same text") == 1.0
    assert similarity_ratio("hello world", "hello there") == similarity_ratio("hello world"[:4000], "hello there"[:4000])


def test_submit_body_draft_fields_are_length_bounded():
    """b149: generated_draft/edited_reply feed similarity_ratio + were uncapped."""
    import pytest
    from pydantic import ValidationError

    from app.api.feedback_routes import SubmitBody
    from app.api.review_queue_routes import ReviewSubmitBody

    with pytest.raises(ValidationError):
        SubmitBody(inbound_text="hi", generated_draft="x" * 50_001, edited_reply="y")
    with pytest.raises(ValidationError):
        ReviewSubmitBody(reply_pair_id=1, inbound_text="hi", generated_draft="y", edited_reply="x" * 50_001)
