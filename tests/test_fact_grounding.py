"""Tests for fact grounding (Phase A3).

Two pieces: a prompt rule that fires when the inbound asks for a concrete fact,
and harvesting facts from drafted mail into memory during the sweep.
"""

from __future__ import annotations

from app.generation.service import _GROUNDING_RULE, _inbound_requests_fact

# --- _inbound_requests_fact ------------------------------------------------


def test_fact_request_detected():
    assert _inbound_requests_fact("What's your address?")
    assert _inbound_requests_fact("When are you available next week?")
    assert _inbound_requests_fact("Could you send me the link to the doc?")
    assert _inbound_requests_fact("What's the price for the Q3 plan?")


def test_non_fact_request_not_detected():
    # A question, but not asking for a concrete fact.
    assert not _inbound_requests_fact("Did you enjoy the conference?")
    # A fact keyword, but no question.
    assert not _inbound_requests_fact("My address is 12 Main St.")
    # Neither.
    assert not _inbound_requests_fact("Thanks for the update.")
    assert not _inbound_requests_fact("")


def test_grounding_rule_appears_in_prompt_when_fact_requested():
    from app.generation.service import assemble_prompt

    prompt = assemble_prompt(
        inbound_message="What is your office address?",
        reply_pairs=[],
        persona={},
        prompts={"draft_system": "You are a helpful assistant."},
        subject="Question",
    )
    assert "[GROUNDING]" in prompt
    assert _GROUNDING_RULE in prompt


def test_grounding_rule_absent_for_ordinary_inbound():
    from app.generation.service import assemble_prompt

    prompt = assemble_prompt(
        inbound_message="Great seeing you last week — let's keep in touch!",
        reply_pairs=[],
        persona={},
        prompts={"draft_system": "You are a helpful assistant."},
        subject="Hello",
    )
    assert "[GROUNDING]" not in prompt
