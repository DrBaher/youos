"""Tests for per-sender-type persona variants."""

from app.generation.service import assemble_prompt


def test_mode_override_voice_in_prompt():
    """When a mode sub-config exists, its voice overrides top-level."""
    persona = {
        "style": {
            "voice": "direct, clear, pragmatic",
            "avg_reply_words": 40,
            "constraints": ["no sycophancy"],
        },
        "modes": {
            "internal": {
                "voice": "casual, direct",
                "avg_reply_words": 25,
            },
        },
    }
    # Simulate mode merge (as generate_draft does)
    mode_config = persona["modes"]["internal"]
    style = persona["style"]
    for key in ("voice", "avg_reply_words"):
        if key in mode_config:
            style[key] = mode_config[key]

    prompt = assemble_prompt(
        inbound_message="test",
        reply_pairs=[],
        persona=persona,
        prompts={},
    )
    assert "casual, direct" in prompt
    assert "~25 words" in prompt
    # Constraints should still be present
    assert "no sycophancy" in prompt


def test_mode_fallback_to_top_level():
    """When no matching mode exists, top-level persona is used."""
    persona = {
        "style": {
            "voice": "direct, clear, pragmatic",
            "avg_reply_words": 40,
            "constraints": [],
        },
        "modes": {
            "internal": {"voice": "casual"},
        },
    }
    # No mode merge for 'external_client' since it's not in modes
    sender_type = "external_client"
    modes = persona.get("modes", {})
    if sender_type in modes:
        for key in ("voice", "avg_reply_words"):
            if key in modes[sender_type]:
                persona["style"][key] = modes[sender_type][key]

    prompt = assemble_prompt(
        inbound_message="test",
        reply_pairs=[],
        persona=persona,
        prompts={},
    )
    assert "direct, clear, pragmatic" in prompt
    assert "~40 words" in prompt


def test_mode_preserves_constraints():
    """Mode merge must never override custom_constraints."""
    persona = {
        "style": {
            "voice": "original",
            "avg_reply_words": 40,
            "constraints": ["be concise", "no filler"],
        },
        "modes": {
            "personal": {
                "voice": "warm, casual",
                "avg_reply_words": 30,
            },
        },
    }
    mode_config = persona["modes"]["personal"]
    for key in ("voice", "avg_reply_words"):
        if key in mode_config:
            persona["style"][key] = mode_config[key]

    prompt = assemble_prompt(
        inbound_message="hey",
        reply_pairs=[],
        persona=persona,
        prompts={},
    )
    assert "be concise" in prompt
    assert "no filler" in prompt
    assert "warm, casual" in prompt


def test_persona_yaml_has_modes():
    """Verify that configs/persona.yaml contains modes section."""
    from pathlib import Path

    import yaml

    persona_path = Path(__file__).resolve().parents[1] / "configs" / "persona.yaml"
    persona = yaml.safe_load(persona_path.read_text(encoding="utf-8"))
    assert "modes" in persona
    assert "internal" in persona["modes"]
    assert "external_client" in persona["modes"]
    assert "personal" in persona["modes"]
    assert persona["modes"]["internal"]["voice"] == "casual, direct"
    assert persona["modes"]["external_client"]["avg_reply_words"] == 50
