"""Natural-language → structured rule parsing."""

from __future__ import annotations

from app.agent.nl_rule import _extract_json, parse_rule_text


def test_parses_clean_json():
    out = '{"match": {"subject_contains": "newsletter", "older_than_days": 7}, "action": "archive", "value": null}'
    r = parse_rule_text("archive newsletters older than a week", complete_fn=lambda p: out)
    assert r["ok"] is True
    assert r["rule"]["action"] == "archive"
    assert r["rule"]["match"]["older_than_days"] == 7


def test_extracts_json_from_prose_and_code_fence():
    obj = '{"match": {"sender": "jane@books.com"}, "action": "label", "value": "Finance"}'
    messy = "Sure! Here is the rule:\n```json\n" + obj + "\n```\nHope that helps."
    r = parse_rule_text("label jane as Finance", complete_fn=lambda p: messy)
    assert r["ok"] is True
    assert r["rule"]["value"] == "Finance"


def test_extract_json_is_string_aware_for_regex_braces():
    # a regex value carries braces INSIDE a JSON string — the scanner must not
    # let those throw off the brace balance.
    out = '{"match": {"subject_regex": "invoice #\\\\d{2,3}"}, "action": "star", "value": null}'
    obj = _extract_json("noise " + out + " trailing")
    assert obj is not None
    assert obj["match"]["subject_regex"] == r"invoice #\d{2,3}"
    r = parse_rule_text("star invoices", complete_fn=lambda p: out)
    assert r["ok"] is True


def test_coerces_stringified_bool_and_number():
    out = '{"match": {"has_attachment": "true", "older_than_days": "30"}, "action": "mark_important", "value": null}'
    r = parse_rule_text("flag old mail with attachments", complete_fn=lambda p: out)
    assert r["ok"] is True
    assert r["rule"]["match"]["has_attachment"] is True
    assert r["rule"]["match"]["older_than_days"] == 30


def test_invalid_rule_returns_best_effort_with_error():
    out = '{"match": {"domain": "@x.com"}, "action": "frobnicate", "value": null}'
    r = parse_rule_text("do something weird", complete_fn=lambda p: out)
    assert r["ok"] is False
    assert r["rule"] is not None        # best-effort, so the UI can let the user fix it
    assert r["error"]


def test_unparseable_output_fails_cleanly():
    r = parse_rule_text("?", complete_fn=lambda p: "I'm not sure what you mean.")
    assert r["ok"] is False
    assert r["rule"] is None
    assert r["error"]


def test_model_error_is_caught():
    def boom(p):
        raise RuntimeError("model down")

    r = parse_rule_text("archive newsletters", complete_fn=boom)
    assert r["ok"] is False
    assert r["rule"] is None


def test_empty_text_short_circuits():
    r = parse_rule_text("   ", complete_fn=lambda p: "should not be called")
    assert r["ok"] is False
    assert r["rule"] is None
