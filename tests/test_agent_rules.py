"""Structured standing-instruction rules."""

from __future__ import annotations

from app.agent.rules import DECLINE_INSTRUCTION, apply_rules, rules_need_intent


def test_decline_rule_by_domain():
    rules = [{"match": {"domain": "@recruiters.com"}, "action": "decline", "value": None}]
    r = apply_rules(rules, sender_email="jo@recruiters.com", domain="recruiters.com",
                    intents=None, cold_outreach=False, base_instructions=None)
    assert not r["skip"]
    assert DECLINE_INSTRUCTION in r["instructions"]
    assert len(r["matched"]) == 1


def test_prepend_rule_folds_in_base_instructions():
    rules = [{"match": {"sender": "client@bigco.com"}, "action": "prepend",
              "value": "CC my partner jane@me.com."}]
    r = apply_rules(rules, sender_email="client@bigco.com", domain="bigco.com",
                    intents=None, cold_outreach=False, base_instructions="I'm OOO today.")
    assert "I'm OOO today." in r["instructions"]
    assert "CC my partner jane@me.com." in r["instructions"]


def test_skip_rule_by_cold_outreach():
    rules = [{"match": {"cold_outreach": True}, "action": "skip", "value": None}]
    r = apply_rules(rules, sender_email="x@y.com", domain="y.com",
                    intents=None, cold_outreach=True, base_instructions=None)
    assert r["skip"] is True


def test_intent_rule_matches_only_with_intent():
    rules = [{"match": {"intent": "meeting_request"}, "action": "prepend",
              "value": "Propose Tue/Thu."}]
    hit = apply_rules(rules, sender_email="a@b.com", domain="b.com",
                      intents=["meeting_request"], cold_outreach=False, base_instructions=None)
    miss = apply_rules(rules, sender_email="a@b.com", domain="b.com",
                       intents=["question"], cold_outreach=False, base_instructions=None)
    assert "Propose Tue/Thu." in hit["instructions"]
    assert miss["instructions"] is None
    assert rules_need_intent(rules) is True


def test_no_match_returns_only_base():
    rules = [{"match": {"domain": "@other.com"}, "action": "decline", "value": None}]
    r = apply_rules(rules, sender_email="me@here.com", domain="here.com",
                    intents=None, cold_outreach=False, base_instructions="base")
    assert r["instructions"] == "base"
    assert not r["skip"]
    assert r["matched"] == []


def test_conditions_are_anded():
    # Both sender AND intent must match.
    rules = [{"match": {"sender": "a@b.com", "intent": "meeting_request"},
              "action": "decline", "value": None}]
    only_sender = apply_rules(rules, sender_email="a@b.com", domain="b.com",
                              intents=["question"], cold_outreach=False, base_instructions=None)
    assert only_sender["matched"] == []
