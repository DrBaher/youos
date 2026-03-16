"""Tests for rule-based subject line fallback."""

from __future__ import annotations

from app.generation.service import _subject_fallback


def test_subject_header_extraction():
    """Extract subject from Subject: header and prepend Re:."""
    text = "Subject: Q2 Roadmap Discussion\n\nHi, can we meet?"
    assert _subject_fallback(text) == "Re: Q2 Roadmap Discussion"


def test_subject_header_strips_re_prefix():
    """Strip existing Re: prefixes from Subject: header."""
    text = "Subject: Re: Re: Q2 Roadmap\n\nSure, let's discuss."
    assert _subject_fallback(text) == "Re: Q2 Roadmap"


def test_subject_header_single_re():
    text = "Subject: Re: Meeting tomorrow\n\nSounds good."
    assert _subject_fallback(text) == "Re: Meeting tomorrow"


def test_first_words_fallback():
    """When no Subject: header, extract topic from substantive lines."""
    text = "Hi John,\n\nCan we push the meeting to next week please?"
    result = _subject_fallback(text)
    assert result is not None
    assert "hi" not in result.lower().split()[0:1]
    assert len(result) >= 3


def test_greeting_stripping():
    """Greeting/filler lines are skipped, topic line extracted."""
    text = "Hello,\nI hope you are well.\nThis is about the project deadline next Friday."
    result = _subject_fallback(text)
    assert result is not None
    first_word = result.split()[0].lower().rstrip(",")
    assert first_word not in {"hello", "dear", "i", "hope"}


def test_payment_followup():
    """Payment follow-up email produces meaningful subject, not greeting words."""
    text = """Hello Baher,
I hope you are well.
I am following up as we are still waiting for both your response and your outstanding payment.
We kindly ask that this is settled as soon as possible.
Thank you in advance.
Warm regards, Eva"""
    result = _subject_fallback(text)
    assert result is not None
    assert "hope" not in result.lower()
    assert "hello" not in result.lower()
    assert len(result) >= 10


def test_short_result_returns_none():
    """Very short input with no substance returns None."""
    text = "Hi"
    result = _subject_fallback(text)
    assert result is None


def test_empty_input_returns_none():
    text = ""
    result = _subject_fallback(text)
    assert result is None


def test_no_header_long_message():
    """Long substantive message extracts topic sentence."""
    text = "We need to finalize the contract terms before the end of the quarter."
    result = _subject_fallback(text)
    assert result is not None
    assert len(result) >= 8


def test_subject_header_too_short():
    """Subject header with empty content after stripping falls through gracefully."""
    text = "Subject: Re:\n\nOk"
    result = _subject_fallback(text)
    assert result is not None or result is None  # shouldn't crash
