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


def test_personal_facts_always_included_and_framed():
    """`personal` facts (family, location) are always injected like user_pref and
    framed as 'About you' so the drafter grounds personal-circumstance replies on
    them (fixes the 'your daughter' mis-attribution on well-wishes)."""
    from app.generation.service import _format_facts_context
    out = _format_facts_context([
        {"type": "personal", "key": "family", "fact": "You have a 4-year-old daughter and a newborn."},
        {"type": "user_pref", "key": "sign-off", "fact": "Best, B"},
    ])
    assert "- About you (family): You have a 4-year-old daughter and a newborn." in out
    assert "Your preference (sign-off)" in out


def test_lookup_facts_returns_personal(tmp_path):
    """lookup_facts always returns type='personal' rows (alongside user_pref)."""
    import sqlite3

    from app.generation.service import lookup_facts
    db = tmp_path / "m.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE memory (id INTEGER PRIMARY KEY, type TEXT, key TEXT, fact TEXT, "
        "tags TEXT DEFAULT '[]', updated_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute("INSERT INTO memory (type,key,fact) VALUES ('personal','home','You live in Vienna 1030.')")
    conn.commit()
    conn.row_factory = sqlite3.Row
    facts = lookup_facts(sender=None, inbound_text="anything", database_url=f"sqlite:///{db}", conn=conn)
    assert any(f["type"] == "personal" and "Vienna" in f["fact"] for f in facts)
