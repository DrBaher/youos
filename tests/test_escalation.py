"""Tests for confidence × stakes escalation (Phase B)."""

from __future__ import annotations

from app.agent.escalation import ActionDecision, assess_stakes, decide_action

# --- assess_stakes ---------------------------------------------------------


def test_high_stakes_keywords():
    assert assess_stakes("Contract for review", "Please sign the agreement.") == "high"
    assert assess_stakes("Invoice #42", "Payment is overdue.") == "high"
    assert assess_stakes("Re: legal", "Our attorney advises...") == "high"
    assert assess_stakes(None, "Can you wire the deposit today?") == "high"


def test_money_amounts_are_high_stakes():
    assert assess_stakes("Quote", "The total is $4,250.") == "high"
    assert assess_stakes("Budget", "around 50k for the project") == "high"


def test_low_stakes_ordinary_mail():
    assert assess_stakes("Coffee?", "Want to grab coffee Thursday?") == "low"
    assert assess_stakes("Hello", "Great seeing you at the conference!") == "low"


def test_word_boundaries_avoid_false_positives():
    # "contractor" / "legalese" shouldn't trip contract / legal.
    assert assess_stakes("Hiring", "We need a contractor for the deck.") == "low"


# --- decide_action ---------------------------------------------------------


def test_high_stakes_forces_ask_even_with_great_draft():
    d = decide_action(
        quality_score=0.99, needs_reply_score=0.95,
        subject="Contract", body="Please countersign the agreement.",
    )
    assert d.action == "ask"
    assert d.stakes == "high"


def test_high_quality_low_stakes_auto_acts():
    d = decide_action(
        quality_score=0.9, needs_reply_score=0.9,
        subject="Coffee", body="Thursday at 2 work?",
    )
    assert d.action == "auto_act"
    assert d.stakes == "low"


def test_low_quality_queues():
    d = decide_action(
        quality_score=0.4, needs_reply_score=0.9,
        subject="Coffee", body="Thursday?",
    )
    assert d.action == "queue"
    assert any("quality" in r for r in d.reasons)


def test_low_confidence_queues():
    d = decide_action(
        quality_score=0.9, needs_reply_score=0.6,
        subject="Coffee", body="Thursday?",
        confidence_floor=0.85,
    )
    assert d.action == "queue"
    assert any("confidence" in r for r in d.reasons)


def test_none_quality_queues():
    d = decide_action(
        quality_score=None, needs_reply_score=0.95,
        subject="Coffee", body="Thursday?",
    )
    assert d.action == "queue"


def test_calibrated_score_preferred_over_raw():
    # Raw score clears the floor, but calibrated does not → queue.
    d = decide_action(
        quality_score=0.9, needs_reply_score=0.95, calibrated_score=0.5,
        subject="Coffee", body="Thursday?", confidence_floor=0.85,
    )
    assert d.action == "queue"


def test_high_stakes_block_can_be_disabled():
    d = decide_action(
        quality_score=0.95, needs_reply_score=0.95,
        subject="Invoice", body="Payment of $10 due.",
        high_stakes_blocks=False,
    )
    # With the block off, a confident high-stakes draft can auto-act.
    assert isinstance(d, ActionDecision)
    assert d.action == "auto_act"
    assert d.stakes == "high"
