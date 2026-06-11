"""Lead-form outreach rules (b232).

A website "New demo request from X" notification arrives from a noreply@ box;
replying to the sender is useless (live review found exactly that draft in the
queue). An ``outreach_draft`` rule instead extracts the prospect's contact
details from the body and queues a NEW outbound draft to the prospect from a
fixed template — hold-gated, composed as a fresh message on push.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.agent.inbox_fetch import InboxMessage
from app.agent.rules import (
    extract_lead_contact,
    match_outreach_rule,
    normalize_rule,
    render_outreach_template,
    validate_rule,
)

# The exact glued format the form mailer produces (HTML table flattened).
_DEMO_BODY = (
    "NEW DEMO REQUEST\n\n"
    "Nameshakhawat Hussain Emailshakhayat1511@gmail.com Phone+8801733724919 "
    "CountryBangladesh ReasonProduct demo Message i want to know your service "
    "about the Lab Test reports. How you can help?"
)

_RULE = {
    "match": {"sender": "noreply@work.example", "subject_contains": "New demo request"},
    "action": "outreach_draft",
    "subject": "Your demo request",
    "value": "Hi {first_name},\n\nThanks for your interest — happy to set up a demo.\n\nBest,\nBaher",
}


# --- validation / normalisation ----------------------------------------------


def test_outreach_rule_validates_and_keeps_subject():
    ok, err = validate_rule(_RULE)
    assert ok, err
    norm = normalize_rule(_RULE)
    assert norm["action"] == "outreach_draft"
    assert norm["subject"] == "Your demo request"


def test_outreach_rule_requires_template():
    ok, err = validate_rule({"match": {"sender": "a@b.c"}, "action": "outreach_draft"})
    assert not ok and "template" in err


def test_outreach_rule_rejects_intent_predicate():
    ok, err = validate_rule(
        {"match": {"intent": "meeting_request"}, "action": "outreach_draft", "value": "x"}
    )
    assert not ok and "intent" in err


# --- contact extraction --------------------------------------------------------


def test_extract_lead_contact_glued_format():
    c = extract_lead_contact(_DEMO_BODY)
    assert c["email"] == "shakhayat1511@gmail.com"
    assert c["name"] == "shakhawat Hussain"
    assert c["first_name"] == "Shakhawat"
    assert c["country"] == "Bangladesh"
    assert c["reason"] == "Product demo"
    assert "Lab Test" in c["message"]


def test_extract_lead_contact_fallback_first_email():
    c = extract_lead_contact("Someone wrote in. Reach them at lead@corp.example for details.")
    assert c["email"] == "lead@corp.example"
    assert c["first_name"] == ""


def test_extract_lead_contact_never_returns_own_address():
    c = extract_lead_contact(
        "Form copy to baher@work.example. Lead: jane@corp.example",
        exclude_emails=["baher@work.example"],
    )
    assert c["email"] == "jane@corp.example"


def test_extract_lead_contact_empty_body():
    assert extract_lead_contact(None)["email"] == ""


# --- template rendering ---------------------------------------------------------


def test_render_fills_placeholders_and_tolerates_missing():
    out = render_outreach_template(
        "Hi {first_name},\n\nRe {reason}: {message}\n\n{unknown_key}Best",
        extract_lead_contact(_DEMO_BODY),
    )
    assert out.startswith("Hi Shakhawat,")
    assert "Product demo" in out
    assert "{" not in out


def test_render_collapses_empty_name_artifact():
    out = render_outreach_template("Hi {first_name},\n\nHello.", {"first_name": ""})
    assert out.startswith("Hi,")


# --- matching runs pre-classification --------------------------------------------


def test_match_outreach_rule_matches_and_filters_action():
    rules = [
        {"match": {"sender": "noreply@work.example"}, "action": "hold", "value": None},
        normalize_rule(_RULE),
    ]
    r = match_outreach_rule(
        rules, sender_email="noreply@work.example", domain="work.example",
        subject="New demo request from shakhawat Hussain — Product demo", body=_DEMO_BODY,
    )
    assert r is not None and r["action"] == "outreach_draft"
    assert match_outreach_rule(
        rules, sender_email="other@x.example", domain="x.example",
        subject="hello", body="hi",
    ) is None


# --- end-to-end through run_triage ------------------------------------------------


@pytest.fixture
def outreach_env(monkeypatch, tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY, inbound_author TEXT)")
    from app.db.bootstrap import _migrate_agent_audit, _migrate_agent_pending_drafts

    _migrate_agent_pending_drafts(conn)
    _migrate_agent_audit(conn)
    conn.commit()
    conn.close()

    msgs = [
        InboxMessage(
            message_id="demo-1",
            thread_id="dt-1",
            account="you@example.com",
            sender="Website <noreply@work.example>",
            sender_email="noreply@work.example",
            subject="New demo request from shakhawat Hussain — Product demo",
            body=_DEMO_BODY,
            headers={},
        ),
    ]
    monkeypatch.setattr("app.agent.triage.fetch_unread", lambda *a, **k: list(msgs))
    monkeypatch.setattr("app.agent.rules.load_rules", lambda: [normalize_rule(_RULE)])
    return {"database_url": f"sqlite:///{db}", "configs_dir": tmp_path}


def test_triage_persists_outreach_draft_to_prospect(outreach_env):
    from app.agent.triage import run_triage

    run_triage(
        account="you@example.com",
        database_url=outreach_env["database_url"],
        configs_dir=outreach_env["configs_dir"],
    )
    conn = sqlite3.connect(outreach_env["database_url"].removeprefix("sqlite:///"))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM agent_pending_drafts").fetchone()
    conn.close()
    assert row is not None
    assert row["tier"] == "draft"
    assert row["outreach"] == 1
    assert row["hold"] == 1  # never auto-push/auto-send a new-recipient outbound
    assert row["sender_email"] == "shakhayat1511@gmail.com"
    assert row["to_recipients"] == "shakhayat1511@gmail.com"
    assert row["subject"] == "Your demo request"
    assert row["draft"].startswith("Hi Shakhawat,")
    assert row["draft_model"] == "rule_template"
    assert "rule: outreach_draft (lead-form notification)" in row["reasons_json"]


def test_triage_outreach_is_idempotent(outreach_env):
    from app.agent.triage import run_triage

    run_triage(account="you@example.com", database_url=outreach_env["database_url"], configs_dir=outreach_env["configs_dir"])
    run_triage(account="you@example.com", database_url=outreach_env["database_url"], configs_dir=outreach_env["configs_dir"])
    conn = sqlite3.connect(outreach_env["database_url"].removeprefix("sqlite:///"))
    n = conn.execute("SELECT count(*) FROM agent_pending_drafts").fetchone()[0]
    conn.close()
    assert n == 1


# --- push composes a NEW message ----------------------------------------------------


def test_push_outreach_row_composes_fresh_message(monkeypatch, tmp_path):
    db = tmp_path / "p.db"
    conn = sqlite3.connect(db)
    from app.db.bootstrap import _migrate_agent_pending_drafts

    _migrate_agent_pending_drafts(conn)
    conn.commit()
    conn.close()
    url = f"sqlite:///{db}"

    from app.agent import store
    from app.agent.push import push_pending_row

    row_id = store.upsert_pending(
        database_url=url,
        message_id="demo-1", thread_id="dt-1", account="you@example.com",
        sender="shakhawat Hussain", sender_email="shakhayat1511@gmail.com",
        subject="Your demo request", body=_DEMO_BODY, received_at=None,
        needs_reply_score=0.6, reasons=["rule: outreach_draft (lead-form notification)"],
        cold_outreach=False, tier="draft",
        draft="Hi Shakhawat,\n\nThanks for your interest.\n\nBest,\nBaher",
        draft_model="rule_template", draft_repairs=[],
        standing_instructions_snapshot=None,
        hold=True, to_recipients="shakhayat1511@gmail.com", outreach=True,
    )

    captured = {}

    class _Result:
        draft_id = "gd-123"

    def _fake_create_draft(**kwargs):
        captured.update(kwargs)
        return _Result()

    from app.ingestion import gmail_write

    monkeypatch.setattr(gmail_write, "create_draft", _fake_create_draft)
    monkeypatch.setattr(gmail_write, "get_signature", lambda **k: None)

    out = push_pending_row(url, row_id)
    assert out.ok
    assert captured["to_email"] == "shakhayat1511@gmail.com"
    assert captured["subject"] == "Your demo request"  # no "Re:" on a fresh message
    assert captured["thread_id"] is None
    assert captured["reply_to_message_id"] is None
    assert captured["cc"] is None


# --- outcome capture skips outreach rows ----------------------------------------------


def test_outcome_capture_skips_outreach_rows(monkeypatch, tmp_path):
    db = tmp_path / "o.db"
    conn = sqlite3.connect(db)
    from app.db.bootstrap import _migrate_agent_pending_drafts

    _migrate_agent_pending_drafts(conn)
    conn.commit()
    conn.close()
    url = f"sqlite:///{db}"

    from app.agent import store
    from app.agent.outcome_capture import capture_send_outcomes

    store.upsert_pending(
        database_url=url,
        message_id="demo-1", thread_id="dt-1", account="you@example.com",
        sender="Lead", sender_email="lead@corp.example",
        subject="Your demo request", body="x", received_at=None,
        needs_reply_score=0.6, reasons=[], cold_outreach=False, tier="draft",
        draft="Hi,", draft_model="rule_template", draft_repairs=[],
        standing_instructions_snapshot=None, hold=True,
        to_recipients="lead@corp.example", outreach=True,
    )
    # The thread-reconciliation source must never be consulted: scanning an
    # outreach row would mark a false no_send (the user's send starts a NEW
    # thread the notification thread can't show).
    monkeypatch.setattr(
        "app.ingestion.adapters.get_google_source",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("source should not be needed")),
    )
    s = capture_send_outcomes(url, account="you@example.com", lookback_days=7)
    assert s["scanned"] == 0
