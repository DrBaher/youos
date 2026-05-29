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


# --- Phase D: content predicates + hold action -----------------------------


def test_body_contains_keyword_list_triggers_hold():
    rules = [{"match": {"body_contains": ["legal", "contract", "lawsuit"]},
              "action": "hold", "value": None}]
    r = apply_rules(rules, sender_email="a@b.com", domain="b.com",
                    intents=None, cold_outreach=False, base_instructions=None,
                    subject="Re: project", body="Please review the attached CONTRACT terms.")
    assert r["hold"] is True
    assert r["skip"] is False


def test_subject_contains_single_keyword():
    rules = [{"match": {"subject_contains": "invoice"}, "action": "hold", "value": None}]
    hit = apply_rules(rules, sender_email="a@b.com", domain="b.com", intents=None,
                      cold_outreach=False, base_instructions=None,
                      subject="Invoice #42 attached", body="see attached")
    miss = apply_rules(rules, sender_email="a@b.com", domain="b.com", intents=None,
                       cold_outreach=False, base_instructions=None,
                       subject="Lunch?", body="invoice")  # keyword in body, not subject
    assert hit["hold"] is True
    assert miss["hold"] is False


def test_content_predicate_anded_with_sender():
    rules = [{"match": {"sender": "boss@co.com", "body_contains": "raise"},
              "action": "hold", "value": None}]
    hit = apply_rules(rules, sender_email="boss@co.com", domain="co.com", intents=None,
                      cold_outreach=False, base_instructions=None,
                      subject="chat", body="let's talk about your raise")
    wrong_sender = apply_rules(rules, sender_email="other@co.com", domain="co.com", intents=None,
                               cold_outreach=False, base_instructions=None,
                               subject="chat", body="let's talk about your raise")
    assert hit["hold"] is True
    assert wrong_sender["hold"] is False


def test_validate_rule_accepts_and_rejects():
    from app.agent.rules import validate_rule

    assert validate_rule({"match": {"domain": "@x.com"}, "action": "label", "value": "Work"})[0]
    assert validate_rule({"match": {"subject_contains": "hi"}, "action": "archive"})[0]
    # bad shapes
    assert not validate_rule({"match": {}, "action": "label", "value": "X"})[0]          # empty match
    assert not validate_rule({"match": {"frm": "x"}, "action": "star"})[0]               # unknown match key
    assert not validate_rule({"match": {"domain": "x"}, "action": "frobnicate"})[0]      # unknown action
    assert not validate_rule({"match": {"domain": "x"}, "action": "label"})[0]           # label w/o value
    assert not validate_rule({"match": {"domain": "x"}, "action": "label", "value": "a,b"})[0]   # comma
    assert not validate_rule({"match": {"domain": "x"}, "action": "label", "value": "YouOS/skip"})[0]  # reserved


def test_save_rules_round_trips_through_config(tmp_path, monkeypatch):
    import app.core.config as config_mod
    from app.agent.rules import load_rules, save_rules

    cfg = tmp_path / "youos_config.yaml"
    cfg.write_text("user:\n  name: T\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    config_mod.load_config.cache_clear()
    try:
        save_rules([
            {"match": {"domain": "@recruiters.com"}, "action": "label", "value": "Recruiting"},
            {"match": {"subject_contains": "invoice"}, "action": "star"},
        ])
        loaded = load_rules()
        assert [r["value"] for r in loaded if r["action"] == "label"] == ["Recruiting"]
        assert any(r["action"] == "star" for r in loaded)
    finally:
        config_mod.load_config.cache_clear()


def test_save_rules_rejects_an_invalid_rule(tmp_path, monkeypatch):
    import app.core.config as config_mod
    from app.agent.rules import save_rules

    cfg = tmp_path / "youos_config.yaml"
    cfg.write_text("user: {}\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    config_mod.load_config.cache_clear()
    try:
        import pytest
        with pytest.raises(ValueError):
            save_rules([{"match": {"domain": "x"}, "action": "label", "value": "a,b"}])
    finally:
        config_mod.load_config.cache_clear()


def test_load_rules_drops_unsafe_label_rules(monkeypatch):
    """A label name with a comma (gog splits comma-delimited --add) or in the
    reserved YouOS/ namespace (would fight the dismissal-label sync) is dropped."""
    from app.agent import rules as rules_mod

    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {"agent": {"rules": [
        {"match": {"domain": "@x.com"}, "action": "label", "value": "a,b"},          # comma → drop
        {"match": {"domain": "@x.com"}, "action": "label", "value": "YouOS/skip"},   # reserved → drop
        {"match": {"domain": "@x.com"}, "action": "label", "value": "Recruiting"},   # ok
        {"match": {"domain": "@x.com"}, "action": "archive", "value": None},          # ok
    ]}})
    loaded = rules_mod.load_rules()
    assert [r["value"] for r in loaded if r["action"] == "label"] == ["Recruiting"]
    assert any(r["action"] == "archive" for r in loaded)


def test_hold_still_drafts_no_skip():
    rules = [{"match": {"body_contains": "legal"}, "action": "hold", "value": None}]
    r = apply_rules(rules, sender_email="a@b.com", domain="b.com", intents=None,
                    cold_outreach=False, base_instructions="base", body="legal stuff")
    assert r["hold"] is True
    assert r["skip"] is False          # hold drafts; it does not drop the message
    assert "base" in r["instructions"]  # instructions still flow through


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
