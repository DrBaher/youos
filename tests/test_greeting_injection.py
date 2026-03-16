"""Tests for greeting/closing injection into prompts (Item 1)."""

from app.generation.service import _resolve_closing, _resolve_greeting, assemble_prompt


def _make_persona(**overrides):
    base = {
        "style": {"voice": "direct", "constraints": []},
        "greeting_patterns": {
            "internal": "Hi {name},",
            "external_client": "Hi {name},",
            "personal": "Hey {name},",
            "default": "Hi,",
        },
        "closing_patterns": {
            "informal": "Cheers,",
            "formal": "Best,",
            "default": "Best,",
        },
        "modes": {
            "internal": {"voice": "casual", "greeting": "Hey {name},", "closing": "Cheers,"},
            "external_client": {"voice": "professional", "greeting": "Hi {name},", "closing": "Best,"},
            "personal": {"voice": "warm", "greeting": "Hey {name},", "closing": "Talk soon,"},
        },
    }
    base.update(overrides)
    return base


def test_resolve_greeting_mode_preferred():
    persona = _make_persona()
    assert _resolve_greeting(persona, "internal", "Sarah") == "Hey Sarah,"


def test_resolve_greeting_fallback_to_pattern():
    persona = _make_persona(modes={})
    assert _resolve_greeting(persona, "internal", "Sarah") == "Hi Sarah,"


def test_resolve_greeting_fallback_to_default():
    persona = _make_persona(modes={}, greeting_patterns={"default": "Hi,"})
    assert _resolve_greeting(persona, "unknown_type", None) == "Hi,"


def test_resolve_greeting_name_substitution_empty():
    persona = _make_persona()
    result = _resolve_greeting(persona, "internal", None)
    assert "{name}" not in result


def test_resolve_closing_mode_preferred():
    persona = _make_persona()
    assert _resolve_closing(persona, "personal") == "Talk soon,"


def test_resolve_closing_fallback_to_default():
    persona = _make_persona(modes={}, closing_patterns={"default": "Best,"})
    assert _resolve_closing(persona, "unknown_type") == "Best,"


def test_greeting_closing_injected_in_prompt():
    persona = _make_persona()
    prompt = assemble_prompt(
        inbound_message="Hi there",
        reply_pairs=[],
        persona=persona,
        prompts={},
        sender_type="internal",
        first_name="Sarah",
    )
    assert "Begin your reply with: Hey Sarah," in prompt
    assert "End your reply with: Cheers," in prompt


def test_no_injection_when_greeting_empty():
    persona = _make_persona(
        modes={},
        greeting_patterns={"default": ""},
        closing_patterns={"default": "Best,"},
    )
    prompt = assemble_prompt(
        inbound_message="Hi",
        reply_pairs=[],
        persona=persona,
        prompts={},
        sender_type="internal",
    )
    assert "Begin your reply with:" not in prompt


def test_no_injection_when_closing_empty():
    persona = _make_persona(
        modes={},
        greeting_patterns={"default": "Hi,"},
        closing_patterns={"default": ""},
    )
    prompt = assemble_prompt(
        inbound_message="Hi",
        reply_pairs=[],
        persona=persona,
        prompts={},
        sender_type="internal",
    )
    assert "End your reply with:" not in prompt


def test_no_injection_when_no_sender_type():
    persona = _make_persona()
    prompt = assemble_prompt(
        inbound_message="Hi",
        reply_pairs=[],
        persona=persona,
        prompts={},
        sender_type=None,
    )
    # Should fall back to default patterns
    assert "Begin your reply with: Hi," in prompt
    assert "End your reply with: Best," in prompt
