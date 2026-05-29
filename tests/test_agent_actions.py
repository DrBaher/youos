"""Tests for the agent-action framework: rule-driven mailbox routing."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from app.agent import actions as act
from app.agent.rules import evaluate_mailbox_actions
from app.db.bootstrap import _migrate_agent_actions
from app.ingestion import gmail_write


def _msg(**over):
    base = dict(message_id="m1", thread_id="t1", account="me@x.com",
               sender="Jo <jo@recruiters.com>", sender_email="jo@recruiters.com",
               subject="Exciting opportunity", body="We have a role for you.")
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    conn = sqlite3.connect(p)
    _migrate_agent_actions(conn)
    conn.commit()
    conn.close()
    return f"sqlite:///{p}"


# --- rules: evaluate_mailbox_actions ---------------------------------------


def test_label_rule_by_domain():
    rules = [{"match": {"domain": "@recruiters.com"}, "action": "label", "value": "Recruiting"}]
    out = evaluate_mailbox_actions(rules, sender_email="jo@recruiters.com", domain="recruiters.com",
                                   subject="hi", body="x")
    assert out == [{"type": "label", "value": "Recruiting"}]


def test_archive_and_star_and_content_predicate():
    rules = [
        {"match": {"subject_contains": "newsletter"}, "action": "archive", "value": None},
        {"match": {"body_contains": ["urgent", "asap"]}, "action": "star", "value": None},
    ]
    out = evaluate_mailbox_actions(rules, sender_email="n@x.com", domain="x.com",
                                   subject="Weekly newsletter", body="please respond ASAP")
    types = {a["type"] for a in out}
    assert types == {"archive", "star"}


def test_label_rule_without_value_is_skipped():
    rules = [{"match": {"domain": "@x.com"}, "action": "label", "value": None}]
    assert evaluate_mailbox_actions(rules, sender_email="a@x.com", domain="x.com", subject="s", body="b") == []


def test_draft_actions_are_not_returned_as_mailbox_actions():
    rules = [{"match": {"domain": "@x.com"}, "action": "skip", "value": None},
             {"match": {"domain": "@x.com"}, "action": "label", "value": "X"}]
    out = evaluate_mailbox_actions(rules, sender_email="a@x.com", domain="x.com", subject="s", body="b")
    assert out == [{"type": "label", "value": "X"}]


def test_duplicate_actions_collapsed():
    rules = [{"match": {"domain": "@x.com"}, "action": "star", "value": None},
             {"match": {"subject_contains": "hi"}, "action": "star", "value": None}]
    out = evaluate_mailbox_actions(rules, sender_email="a@x.com", domain="x.com", subject="hi", body="b")
    assert out == [{"type": "star", "value": None}]


# --- gmail_write command shapes --------------------------------------------


def test_modify_builds_verified_gog_command(monkeypatch):
    seen = {}

    class _R:
        returncode = 0
        stdout = '{"id":"m1"}'
        stderr = ""

    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run",
                        lambda cmd, **kw: seen.update(cmd=cmd) or _R())
    gmail_write.modify_message_labels(account="me@x.com", message_id="m1", add=["Recruiting"], remove=["INBOX"])
    cmd = seen["cmd"]
    assert cmd[:5] == ["gog", "gmail", "messages", "modify", "m1"]
    assert "--add" in cmd and "Recruiting" in cmd
    assert "--remove" in cmd and "INBOX" in cmd


def test_ensure_label_creates_when_missing(monkeypatch):
    calls = []
    monkeypatch.setattr(gmail_write, "list_labels", lambda *, account: {"INBOX", "Work"})

    class _R:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def _run(cmd, **kw):
        calls.append(cmd)
        return _R()

    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", _run)
    gmail_write.ensure_label(account="me@x.com", name="Recruiting")   # missing → create
    gmail_write.ensure_label(account="me@x.com", name="Work")         # exists → no create
    gmail_write.ensure_label(account="me@x.com", name="STARRED")      # system → no create
    created = [c for c in calls if "create" in c]
    assert len(created) == 1 and "Recruiting" in created[0]


# --- executor: gating / dry-run / live / idempotency / cap / undo ----------


def _cfg(enabled=True, dry_run=False, cap=50):
    return {"enabled": enabled, "dry_run": dry_run, "daily_cap": cap}


def test_disabled_is_noop(db, monkeypatch):
    monkeypatch.setattr(act, "_actions_config", lambda: _cfg(enabled=False))
    monkeypatch.setattr(gmail_write, "modify_message_labels",
                        lambda **k: (_ for _ in ()).throw(AssertionError("must not touch Gmail when disabled")))
    assert act.apply_mailbox_actions(db, "me@x.com", _msg(), [{"type": "label", "value": "X"}]) == []


def test_dry_run_records_without_touching_gmail(db, monkeypatch):
    monkeypatch.setattr(act, "_actions_config", lambda: _cfg(dry_run=True))
    monkeypatch.setattr(gmail_write, "modify_message_labels",
                        lambda **k: (_ for _ in ()).throw(AssertionError("dry-run must not touch Gmail")))
    res = act.apply_mailbox_actions(db, "me@x.com", _msg(), [{"type": "label", "value": "Recruiting"}])
    assert res[0]["status"] == "dry_run"
    ledger = act.list_actions(db)
    assert len(ledger) == 1 and ledger[0]["status"] == "dry_run"


def test_live_applies_and_is_idempotent(db, monkeypatch):
    monkeypatch.setattr(act, "_actions_config", lambda: _cfg())
    calls = {"n": 0}
    monkeypatch.setattr(gmail_write, "ensure_label", lambda **k: None)
    monkeypatch.setattr(gmail_write, "modify_message_labels",
                        lambda **k: calls.update(n=calls["n"] + 1) or gmail_write.GmailModifyResult("m1", k.get("add", []), k.get("remove", []), {}))
    a = [{"type": "label", "value": "Recruiting"}]
    first = act.apply_mailbox_actions(db, "me@x.com", _msg(), a)
    second = act.apply_mailbox_actions(db, "me@x.com", _msg(), a)  # same msg+action again
    assert first[0]["status"] == "applied"
    assert second[0]["status"] == "skipped_done"
    assert calls["n"] == 1  # applied to Gmail exactly once


def test_dry_run_does_not_block_later_live(db, monkeypatch):
    # dry-run logs intent; a later live run must still apply (not skipped_done).
    monkeypatch.setattr(act, "_actions_config", lambda: _cfg(dry_run=True))
    act.apply_mailbox_actions(db, "me@x.com", _msg(), [{"type": "star", "value": None}])
    monkeypatch.setattr(act, "_actions_config", lambda: _cfg(dry_run=False))
    monkeypatch.setattr(gmail_write, "modify_message_labels",
                        lambda **k: gmail_write.GmailModifyResult("m1", k.get("add", []), [], {}))
    res = act.apply_mailbox_actions(db, "me@x.com", _msg(), [{"type": "star", "value": None}])
    assert res[0]["status"] == "applied"


def test_daily_cap_skips_excess(db, monkeypatch):
    monkeypatch.setattr(act, "_actions_config", lambda: _cfg())
    monkeypatch.setattr(gmail_write, "ensure_label", lambda **k: None)
    monkeypatch.setattr(gmail_write, "modify_message_labels",
                        lambda **k: gmail_write.GmailModifyResult("m", [], [], {}))
    res = act.apply_mailbox_actions(db, "me@x.com", _msg(),
                                    [{"type": "label", "value": "A"}, {"type": "star", "value": None}],
                                    remaining=1)
    statuses = [r["status"] for r in res]
    assert statuses.count("applied") == 1 and statuses.count("skipped_cap") == 1


def test_error_is_recorded_not_raised(db, monkeypatch):
    monkeypatch.setattr(act, "_actions_config", lambda: _cfg())
    monkeypatch.setattr(gmail_write, "ensure_label", lambda **k: None)
    monkeypatch.setattr(gmail_write, "modify_message_labels",
                        lambda **k: (_ for _ in ()).throw(gmail_write.GmailWriteError("gog boom")))
    res = act.apply_mailbox_actions(db, "me@x.com", _msg(), [{"type": "label", "value": "X"}])
    assert res[0]["status"] == "error"
    assert act.list_actions(db)[0]["status"] == "error"


def test_undo_reverses_applied_action(db, monkeypatch):
    monkeypatch.setattr(act, "_actions_config", lambda: _cfg())
    monkeypatch.setattr(gmail_write, "ensure_label", lambda **k: None)
    seen = []
    monkeypatch.setattr(gmail_write, "modify_message_labels",
                        lambda **k: seen.append((k.get("add"), k.get("remove"))) or gmail_write.GmailModifyResult("m1", [], [], {}))
    act.apply_mailbox_actions(db, "me@x.com", _msg(), [{"type": "archive", "value": None}])  # remove INBOX
    aid = act.list_actions(db)[0]["id"]
    out = act.undo_action(db, aid)
    assert out["ok"]
    # archive removed INBOX; undo must RE-ADD INBOX.
    assert seen[-1] == (["INBOX"], [])
    assert act.get_action(db, aid)["status"] == "undone"


def test_undo_rejects_non_applied(db, monkeypatch):
    monkeypatch.setattr(act, "_actions_config", lambda: _cfg(dry_run=True))
    act.apply_mailbox_actions(db, "me@x.com", _msg(), [{"type": "star", "value": None}])
    aid = act.list_actions(db)[0]["id"]
    out = act.undo_action(db, aid)
    assert not out["ok"] and out["http_status"] == 409


# --- sweep wiring: _maybe_apply_mailbox_actions ----------------------------


def test_sweep_routes_every_fetched_message(db, monkeypatch):
    """The sweep applies routing rules to ALL fetched messages (not just drafts)
    and enforces the daily cap across them."""
    from app.agent import triage
    from app.agent.inbox_fetch import InboxMessage

    monkeypatch.setattr(act, "_actions_config", lambda: _cfg(cap=50))
    monkeypatch.setattr("app.agent.rules.load_rules", lambda: [
        {"match": {"domain": "@recruiters.com"}, "action": "label", "value": "Recruiting"},
        {"match": {"subject_contains": "newsletter"}, "action": "archive", "value": None},
    ])
    applied = []
    monkeypatch.setattr(gmail_write, "ensure_label", lambda **k: None)

    def _mod(**k):
        applied.append((k["message_id"], k.get("add"), k.get("remove")))
        return gmail_write.GmailModifyResult(k["message_id"], [], [], {})

    monkeypatch.setattr(gmail_write, "modify_message_labels", _mod)

    msgs = [
        InboxMessage(message_id="r1", thread_id="t1", account="me@x.com",
                     sender="Jo <jo@recruiters.com>", sender_email="jo@recruiters.com",
                     subject="A role for you", body="hi", headers={}),
        InboxMessage(message_id="n1", thread_id="t2", account="me@x.com",
                     sender="News <news@x.com>", sender_email="news@x.com",
                     subject="Weekly newsletter", body="stories", headers={}),
        InboxMessage(message_id="p1", thread_id="t3", account="me@x.com",
                     sender="Pat <pat@partner.com>", sender_email="pat@partner.com",
                     subject="Quick question", body="?", headers={}),  # matches nothing
    ]
    out = triage._maybe_apply_mailbox_actions(db, "me@x.com", msgs)
    by_msg = {m: a for (m, a, _r) in applied}
    assert ("r1", ["Recruiting"], []) in applied   # recruiter → labeled
    assert ("n1", [], ["INBOX"]) in applied        # newsletter → archived
    assert "p1" not in by_msg                       # no rule matched
    assert sum(1 for r in out if r["status"] == "applied") == 2
