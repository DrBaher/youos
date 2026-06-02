"""Tests for per-mode max_tokens in generation (Item 8).

b187: the budget is now derived from the persona length BAND (upper-edge tokens
+ headroom), not avg×5. These tests pin the resolution PRIORITY (mode > intent >
global > default) — the part that actually matters — by comparing against the
band budget of the *expected* effective words, not a hardcoded ×5 number.
"""

from app.generation.service import (
    _MAX_TOKENS_HEADROOM,
    _TOKENS_PER_WORD,
    _compute_max_tokens,
    _length_band,
)


def _budget(effective_words):
    """The band-derived budget b187's _compute_max_tokens should produce."""
    band = _length_band(effective_words)
    if band is None:
        return 300
    _lo, hi = band
    return max(100, min(500, int(round(hi * _TOKENS_PER_WORD * _MAX_TOKENS_HEADROOM))))


def test_default_no_persona():
    assert _compute_max_tokens(None) == 300


def test_with_avg_words():
    assert _compute_max_tokens(40) == _budget(40)
    assert _compute_max_tokens(40) != 200  # not the old avg×5


def test_mode_specific_override():
    persona = {
        "_active_mode_config": {"avg_reply_words": 25},
        "style": {"avg_reply_words": 40},
    }
    assert _compute_max_tokens(40, persona=persona) == _budget(25)  # mode wins


def test_intent_override_when_no_mode():
    persona = {
        "_active_mode_config": {},
        "style": {"avg_reply_words": 40, "intent_avg_words": {"thank_you": 12}},
    }
    result = _compute_max_tokens(40, persona=persona, intent="thank_you")
    assert result == _budget(12)


def test_mode_beats_intent():
    persona = {
        "_active_mode_config": {"avg_reply_words": 25},
        "style": {"avg_reply_words": 40, "intent_avg_words": {"thank_you": 12}},
    }
    result = _compute_max_tokens(40, persona=persona, intent="thank_you")
    assert result == _budget(25)  # mode wins over intent


def test_global_fallback():
    persona = {
        "_active_mode_config": {},
        "style": {"avg_reply_words": 50},
    }
    assert _compute_max_tokens(None, persona=persona) == _budget(50)


def test_default_fallback():
    persona = {
        "_active_mode_config": {},
        "style": {},
    }
    assert _compute_max_tokens(None, persona=persona) == 300


def test_min_clamp():
    persona = {
        "_active_mode_config": {"avg_reply_words": 5},
        "style": {},
    }
    assert _compute_max_tokens(None, persona=persona) == 100  # floored


def test_max_clamp():
    persona = {
        "_active_mode_config": {"avg_reply_words": 200},
        "style": {},
    }
    assert _compute_max_tokens(None, persona=persona) == 500  # ceiled
