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


def test_resolve_greeting_empty_name_never_produces_hi_comma_artifact():
    """Regression: when first_name extraction fails (None/empty), the greeting
    template's leading space + placeholder must collapse cleanly — never
    "Hi , " or "Hey , " with a dangling space before the comma. Reported by
    QA review against BaherOS drafts."""
    persona = _make_persona()
    for sender_type in ("internal", "external_client", "personal"):
        for empty in (None, ""):
            result = _resolve_greeting(persona, sender_type, empty)
            assert " ," not in result, (
                f"greeting for sender_type={sender_type!r}, name={empty!r} "
                f"left a space before the comma: {result!r}"
            )
            assert "  " not in result, (
                f"greeting collapsed to a double space: {result!r}"
            )
            # And it doesn't drop the punctuation entirely either — the user
            # still sees a recognisable greeting like "Hi," / "Hey,".
            assert result and (result.endswith(",") or result.endswith(":") or "," in result), (
                f"greeting collapsed to junk: {result!r}"
            )


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


# --- b222: flat greeting_style/closing fallback + internal exclusion ----------

# The real BaherOS persona shape: a flat greeting_style + closing_*; modes exist
# but their greeting/closing are unset (None).
def _flat_persona():
    return {
        "greeting_style": "Hi [name],",
        "closing_formal": "Best,\n{name}",
        "closing_informal": "Cheers,\n{name}",
        "modes": {
            "internal": {"greeting": None, "closing": None},
            "client": {"greeting": None, "closing": None},
            "personal": {"greeting": None, "closing": None},
        },
    }


def test_flat_greeting_style_used_for_external(monkeypatch):
    monkeypatch.setattr("app.generation.service._signoff_name", lambda: "Baher")
    p = _flat_persona()
    # external_client maps to the "client" mode (both unset) → falls back to greeting_style.
    assert _resolve_greeting(p, "external_client", "Alice") == "Hi Alice,"
    assert _resolve_closing(p, "external_client") == "Best,\nBaher"


def test_flat_personal_uses_informal_closing(monkeypatch):
    monkeypatch.setattr("app.generation.service._signoff_name", lambda: "Baher")
    p = _flat_persona()
    assert _resolve_greeting(p, "personal", "Sam") == "Hi Sam,"
    assert _resolve_closing(p, "personal") == "Cheers,\nBaher"


def test_internal_gets_no_flat_fallback():
    # With nothing explicitly configured for internal, colleagues get no
    # greeting/closing (user policy: greet everyone EXCEPT internal).
    p = _flat_persona()
    assert _resolve_greeting(p, "internal", "Nadine") == ""
    assert _resolve_closing(p, "internal") == ""


def test_internal_explicit_greeting_still_honored():
    p = _flat_persona()
    p["modes"]["internal"] = {"greeting": "Hey {name},", "closing": "Cheers,"}
    assert _resolve_greeting(p, "internal", "Nadine") == "Hey Nadine,"
    assert _resolve_closing(p, "internal") == "Cheers,"
