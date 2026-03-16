"""Tests for intent classification."""

from app.core.intent import INTENTS, classify_intent


def test_meeting_request():
    assert classify_intent("Can we schedule a meeting for next Tuesday?") == "meeting_request"


def test_approval_needed():
    assert classify_intent("I need your approval on the budget proposal") == "approval_needed"


def test_information_request():
    assert classify_intent("Could you send me the latest report?") == "information_request"


def test_status_update():
    assert classify_intent("Just a quick update on the project progress") == "status_update"


def test_complaint():
    assert classify_intent("I'm frustrated with the broken integration") == "complaint"


def test_thank_you():
    assert classify_intent("Thanks so much for your help with the project") == "thank_you"


def test_urgent():
    assert classify_intent("This is urgent — we need a fix ASAP") == "urgent"


def test_general_empty():
    assert classify_intent("") == "general"


def test_general_no_keywords():
    assert classify_intent("Hello there") == "general"


def test_proposal():
    assert classify_intent("I have a proposal for the new architecture") == "proposal"


def test_introduction():
    assert classify_intent("I'm introducing you to our new colleague, putting you in touch") == "introduction"


def test_intents_dict_has_all_keys():
    expected = {
        "meeting_request",
        "approval_needed",
        "information_request",
        "status_update",
        "introduction",
        "complaint",
        "thank_you",
        "proposal",
        "urgent",
        "general",
    }
    assert set(INTENTS.keys()) == expected


def test_intent_in_assemble_prompt():
    """Intent hint should appear in the assembled prompt."""
    from app.generation.service import assemble_prompt

    prompt = assemble_prompt(
        inbound_message="Can we meet?",
        reply_pairs=[],
        persona={"style": {"voice": "direct", "constraints": []}},
        prompts={},
        intent_hint="meeting_request",
    )
    assert "Email intent: meeting_request" in prompt


def test_intent_general_not_in_prompt():
    """General intent should not clutter the prompt."""
    from app.generation.service import assemble_prompt

    prompt = assemble_prompt(
        inbound_message="Hello",
        reply_pairs=[],
        persona={"style": {"voice": "direct", "constraints": []}},
        prompts={},
        intent_hint="general",
    )
    assert "Email intent:" not in prompt
