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
