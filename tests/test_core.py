"""Tests for core utilities."""
from app.core.diff import is_meaningfully_different, similarity_ratio
from app.core.sender import classify_sender, extract_domain
from app.core.text_utils import decode_html_entities, strip_quoted_text

# ── diff tests ──

def test_similarity_identical():
    assert similarity_ratio("hello", "hello") == 1.0


def test_similarity_empty():
    assert similarity_ratio("", "") == 1.0


def test_similarity_different():
    ratio = similarity_ratio("hello world", "goodbye universe")
    assert ratio < 0.5


def test_meaningfully_different():
    assert is_meaningfully_different("draft A", "actual reply B") is True


def test_not_meaningfully_different():
    assert is_meaningfully_different("same text", "same text") is False


# ── text_utils tests ──

def test_strip_quoted_text():
    text = "My reply here which is long enough to pass the minimum length check.\n\nOn Mon, Jan 1, 2025, Alice wrote:\nOriginal message"
    result = strip_quoted_text(text)
    assert "My reply here" in result
    assert "Alice wrote" not in result


def test_strip_quoted_short_fallback():
    text = "OK\n\nOn Mon, Jan 1, 2025, Alice wrote:\nOriginal message"
    result = strip_quoted_text(text)
    # Should keep original because stripped is too short
    assert "Alice wrote" in result


def test_decode_html_entities():
    assert decode_html_entities("&amp; &lt; &gt;") == "& < >"


# ── sender tests ──

def test_classify_sender_personal():
    assert classify_sender("alice@gmail.com") == "personal"


def test_classify_sender_automated():
    assert classify_sender("noreply@company.com") == "automated"


def test_classify_sender_unknown():
    assert classify_sender(None) == "unknown"
    assert classify_sender("") == "unknown"


def test_classify_sender_external():
    assert classify_sender("john@somecompany.com") == "external_client"


def test_extract_domain():
    assert extract_domain("Alice <alice@example.com>") == "example.com"
    assert extract_domain(None) is None
    assert extract_domain("no-email-here") is None
