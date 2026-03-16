"""Tests for per-mode max_tokens in generation (Item 8)."""

from app.generation.service import _compute_max_tokens


def test_default_no_persona():
    assert _compute_max_tokens(None) == 300


def test_with_avg_words():
    assert _compute_max_tokens(40) == 200


def test_mode_specific_override():
    persona = {
        "_active_mode_config": {"avg_reply_words": 25},
        "style": {"avg_reply_words": 40},
    }
    result = _compute_max_tokens(40, persona=persona)
    assert result == 125  # 25 * 5


def test_intent_override_when_no_mode():
    persona = {
        "_active_mode_config": {},
        "style": {"avg_reply_words": 40, "intent_avg_words": {"thank_you": 12}},
    }
    result = _compute_max_tokens(40, persona=persona, intent="thank_you")
    assert result == 100  # max(100, 12*5)


def test_mode_beats_intent():
    persona = {
        "_active_mode_config": {"avg_reply_words": 25},
        "style": {"avg_reply_words": 40, "intent_avg_words": {"thank_you": 12}},
    }
    result = _compute_max_tokens(40, persona=persona, intent="thank_you")
    assert result == 125  # mode wins: 25 * 5


def test_global_fallback():
    persona = {
        "_active_mode_config": {},
        "style": {"avg_reply_words": 50},
    }
    result = _compute_max_tokens(None, persona=persona)
    assert result == 250  # 50 * 5


def test_default_fallback():
    persona = {
        "_active_mode_config": {},
        "style": {},
    }
    result = _compute_max_tokens(None, persona=persona)
    assert result == 300


def test_min_clamp():
    persona = {
        "_active_mode_config": {"avg_reply_words": 5},
        "style": {},
    }
    result = _compute_max_tokens(None, persona=persona)
    assert result == 100  # max(100, 5*5)


def test_max_clamp():
    persona = {
        "_active_mode_config": {"avg_reply_words": 200},
        "style": {},
    }
    result = _compute_max_tokens(None, persona=persona)
    assert result == 500  # min(500, 200*5)
