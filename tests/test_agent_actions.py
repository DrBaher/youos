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


def test_richer_actions_evaluate_and_map_to_labels():
    """mark_read / mark_important / mark_unimportant are valid routing actions
    and map to the right reversible label add/remove."""
    rules = [
        {"match": {"domain": "@x.com"}, "action": "mark_read", "value": None},
        {"match": {"domain": "@x.com"}, "action": "mark_important", "value": None},
        {"match": {"domain": "@x.com"}, "action": "mark_unimportant", "value": None},
    ]
    out = evaluate_mailbox_actions(rules, sender_email="a@x.com", domain="x.com", subject="s", body="b")
    assert {a["type"] for a in out} == {"mark_read", "mark_important", "mark_unimportant"}

    assert act._action_to_labels({"type": "mark_read"}) == ([], ["UNREAD"])
    assert act._action_to_labels({"type": "mark_important"}) == (["IMPORTANT"], [])
    assert act._action_to_labels({"type": "mark_unimportant"}) == ([], ["IMPORTANT"])
    # undo is the swap
    assert act._reverse_labels({"type": "mark_read"}) == (["UNREAD"], [])
    assert act._reverse_labels({"type": "mark_important"}) == ([], ["IMPORTANT"])


def test_routing_gate_not_blocked_by_new_action_only_rules(db, monkeypatch):
    """Regression: the routing-enable gate must recognise the new actions, so a
    rule set using ONLY mark_read (no label/archive/star) still routes."""
    from app.agent import rules as rules_mod
    from app.agent import triage

    monkeypatch.setattr(act, "_actions_config", lambda: _cfg(dry_run=True))
    monkeypatch.setattr(rules_mod, "load_rules",
                        lambda: [{"match": {"domain": "@recruiters.com"}, "action": "mark_read", "value": None}])
    msg = _msg(headers={}, received_at=None, has_attachment=False)
    out = triage._maybe_apply_mailbox_actions(db, "me@x.com", [msg])
    assert out and out[0]["action"]["type"] == "mark_read"
    assert out[0]["status"] == "dry_run"


def test_mark_read_applies_and_undo_re_adds_unread(db, monkeypatch):
    monkeypatch.setattr(act, "_actions_config", lambda: _cfg())
    monkeypatch.setattr(gmail_write, "ensure_label", lambda **k: None)
    seen = []
    monkeypatch.setattr(gmail_write, "modify_message_labels",
                        lambda **k: seen.append((k.get("add"), k.get("remove"))) or gmail_write.GmailModifyResult("m1", [], [], {}))
    act.apply_mailbox_actions(db, "me@x.com", _msg(), [{"type": "mark_read", "value": None}])
    assert seen[-1] == ([], ["UNREAD"])          # cleared the unread flag
    aid = act.list_actions(db)[0]["id"]
    assert act.undo_action(db, aid)["ok"]
    assert seen[-1] == (["UNREAD"], [])          # undo re-adds UNREAD
    assert act.get_action(db, aid)["status"] == "undone"


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


def test_undone_action_is_not_reapplied_next_sweep(db, monkeypatch):
    """The HIGH bug: after undo, the next sweep must NOT silently re-apply the
    action the user just undid."""
    monkeypatch.setattr(act, "_actions_config", lambda: _cfg())
    monkeypatch.setattr(gmail_write, "ensure_label", lambda **k: None)
    monkeypatch.setattr(gmail_write, "modify_message_labels",
                        lambda **k: gmail_write.GmailModifyResult("m1", [], [], {}))
    a = [{"type": "star", "value": None}]
    act.apply_mailbox_actions(db, "me@x.com", _msg(), a)        # applied
    aid = act.list_actions(db)[0]["id"]
    act.undo_action(db, aid)                                    # undone
    res = act.apply_mailbox_actions(db, "me@x.com", _msg(), a)  # next sweep, same msg+action
    assert res[0]["status"] == "skipped_done"                   # NOT re-applied


def test_double_undo_is_rejected(db, monkeypatch):
    monkeypatch.setattr(act, "_actions_config", lambda: _cfg())
    monkeypatch.setattr(gmail_write, "ensure_label", lambda **k: None)
    monkeypatch.setattr(gmail_write, "modify_message_labels",
                        lambda **k: gmail_write.GmailModifyResult("m1", [], [], {}))
    act.apply_mailbox_actions(db, "me@x.com", _msg(), [{"type": "archive", "value": None}])
    aid = act.list_actions(db)[0]["id"]
    assert act.undo_action(db, aid)["ok"]
    second = act.undo_action(db, aid)
    assert not second["ok"] and second["http_status"] == 409


def test_ensure_label_uses_known_cache(monkeypatch):
    monkeypatch.setattr(gmail_write, "list_labels",
                        lambda **k: (_ for _ in ()).throw(AssertionError("must not list when known is provided")))

    class _R:
        returncode = 0
        stdout = "{}"
        stderr = ""

    created = []
    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", lambda cmd, **kw: created.append(cmd) or _R())
    known = {"INBOX", "Work"}
    gmail_write.ensure_label(account="me@x.com", name="New", known=known)
    assert "New" in known and any("create" in c for c in created)  # created + cached
    created.clear()
    gmail_write.ensure_label(account="me@x.com", name="New", known=known)
    assert created == []  # second call sees the cache, no create


def test_sweep_cap_zero_disables_routing(db, monkeypatch):
    from app.agent import triage
    from app.agent.inbox_fetch import InboxMessage

    monkeypatch.setattr(act, "_actions_config", lambda: {"enabled": True, "dry_run": False, "daily_cap": 0})
    monkeypatch.setattr("app.agent.rules.load_rules",
                        lambda: [{"match": {"domain": "@x.com"}, "action": "star", "value": None}])
    monkeypatch.setattr(gmail_write, "modify_message_labels",
                        lambda **k: (_ for _ in ()).throw(AssertionError("cap=0 must apply nothing")))
    msgs = [InboxMessage(message_id="z1", thread_id="t", account="me@x.com",
                         sender="A <a@x.com>", sender_email="a@x.com", subject="s", body="b", headers={})]
    assert triage._maybe_apply_mailbox_actions(db, "me@x.com", msgs) == []


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


# --- outbound forward action (b113) ----------------------------------------


def _fwd_cfg(enabled=True, dry_run=False, cap=50, allow_forward=True,
             send_enabled=True, kill_switch=False):
    return {"enabled": enabled, "dry_run": dry_run, "daily_cap": cap,
            "allow_forward": allow_forward, "send_enabled": send_enabled,
            "kill_switch": kill_switch}


def _stub_forward(monkeypatch):
    """Record forward_message calls; return a successful result."""
    calls = []

    def _fwd(*, account, message_id, to, note=None, backend=None):
        calls.append({"account": account, "message_id": message_id, "to": to})
        return gmail_write.GmailForwardResult(message_id="sent1", to=to, raw_response={"id": "sent1"})

    monkeypatch.setattr(gmail_write, "forward_message", _fwd)
    return calls


def test_forward_dry_run_records_without_sending(db, monkeypatch):
    monkeypatch.setattr(act, "_forward_config", lambda: _fwd_cfg(dry_run=True))
    calls = _stub_forward(monkeypatch)
    res = act.apply_outbound_actions(db, "me@x.com", _msg(), [{"type": "forward", "value": "j@b.com"}])
    assert res[0]["status"] == "dry_run"
    assert calls == []                       # nothing was sent
    assert act.list_actions(db)[0]["status"] == "dry_run"


def test_forward_blocked_when_gates_closed(db, monkeypatch):
    calls = _stub_forward(monkeypatch)
    # allow_forward off
    monkeypatch.setattr(act, "_forward_config", lambda: _fwd_cfg(allow_forward=False))
    r1 = act.apply_outbound_actions(db, "me@x.com", _msg(message_id="a"), [{"type": "forward", "value": "j@b.com"}])
    # send.enabled off
    monkeypatch.setattr(act, "_forward_config", lambda: _fwd_cfg(send_enabled=False))
    r2 = act.apply_outbound_actions(db, "me@x.com", _msg(message_id="b"), [{"type": "forward", "value": "j@b.com"}])
    # kill switch on
    monkeypatch.setattr(act, "_forward_config", lambda: _fwd_cfg(kill_switch=True))
    r3 = act.apply_outbound_actions(db, "me@x.com", _msg(message_id="c"), [{"type": "forward", "value": "j@b.com"}])
    assert r1[0]["status"] == "blocked" and r2[0]["status"] == "blocked" and r3[0]["status"] == "blocked"
    assert calls == []                       # never sent under any closed gate


def test_forward_live_sends_and_is_at_most_once(db, monkeypatch):
    monkeypatch.setattr(act, "_forward_config", lambda: _fwd_cfg())  # all gates open, live
    calls = _stub_forward(monkeypatch)
    a = [{"type": "forward", "value": "j@b.com"}]
    first = act.apply_outbound_actions(db, "me@x.com", _msg(), a)
    second = act.apply_outbound_actions(db, "me@x.com", _msg(), a)   # next sweep, same msg+dest
    assert first[0]["status"] == "applied"
    assert second[0]["status"] == "skipped_done"
    assert len(calls) == 1 and calls[0]["to"] == "j@b.com"           # sent EXACTLY once


def test_forward_error_is_not_retried(db, monkeypatch):
    monkeypatch.setattr(act, "_forward_config", lambda: _fwd_cfg())
    n = {"i": 0}

    def _boom(*, account, message_id, to, note=None, backend=None):
        n["i"] += 1
        raise gmail_write.GmailWriteError("smtp boom")

    monkeypatch.setattr(gmail_write, "forward_message", _boom)
    a = [{"type": "forward", "value": "j@b.com"}]
    first = act.apply_outbound_actions(db, "me@x.com", _msg(), a)
    second = act.apply_outbound_actions(db, "me@x.com", _msg(), a)
    assert first[0]["status"] == "error"
    assert second[0]["status"] == "skipped_done"   # at-most-once: errored forward NOT retried
    assert n["i"] == 1


def test_forward_distinct_destinations_each_send_once(db, monkeypatch):
    monkeypatch.setattr(act, "_forward_config", lambda: _fwd_cfg())
    calls = _stub_forward(monkeypatch)
    act.apply_outbound_actions(db, "me@x.com", _msg(), [{"type": "forward", "value": "a@x.com"}])
    act.apply_outbound_actions(db, "me@x.com", _msg(), [{"type": "forward", "value": "b@x.com"}])
    assert sorted(c["to"] for c in calls) == ["a@x.com", "b@x.com"]


def test_forward_cannot_be_undone(db, monkeypatch):
    monkeypatch.setattr(act, "_forward_config", lambda: _fwd_cfg())
    _stub_forward(monkeypatch)
    act.apply_outbound_actions(db, "me@x.com", _msg(), [{"type": "forward", "value": "j@b.com"}])
    aid = act.list_actions(db)[0]["id"]
    assert act.get_action(db, aid)["status"] == "applied"
    out = act.undo_action(db, aid)
    assert not out["ok"] and out["http_status"] == 409   # irreversible


def test_forward_disabled_actions_is_noop(db, monkeypatch):
    monkeypatch.setattr(act, "_forward_config", lambda: _fwd_cfg(enabled=False))
    calls = _stub_forward(monkeypatch)
    assert act.apply_outbound_actions(db, "me@x.com", _msg(), [{"type": "forward", "value": "j@b.com"}]) == []
    assert calls == []


def test_gog_forward_builds_verified_command(monkeypatch):
    seen = {}

    class _R:
        returncode = 0
        stdout = '{"id":"sent9"}'
        stderr = ""

    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run",
                        lambda cmd, **kw: seen.update(cmd=cmd) or _R())
    res = gmail_write.forward_message(account="me@x.com", message_id="m1", to="j@b.com", backend="gog")
    cmd = seen["cmd"]
    assert cmd[:4] == ["gog", "gmail", "forward", "m1"]
    assert "--to" in cmd and "j@b.com" in cmd
    assert "--account" in cmd and "--json" in cmd and "--no-input" in cmd
    assert res.message_id == "sent9" and res.to == "j@b.com"


def test_forward_claim_is_atomic_cross_process(db):
    """The audit's TOCTOU fix: the 'forwarding' claim is DB-enforced (partial
    UNIQUE index), so a second concurrent claim for the same (message, dest)
    loses and returns None — it must not go on to send."""
    action = {"type": "forward", "value": "j@b.com"}
    first = act._claim_forward(db, account="me@x.com", message=_msg(), action=action)
    second = act._claim_forward(db, account="me@x.com", message=_msg(), action=action)
    assert first is not None
    assert second is None                       # lost the race → caller won't send
    # a DIFFERENT destination is independent (own claim)
    other = act._claim_forward(db, account="me@x.com", message=_msg(), action={"type": "forward", "value": "k@b.com"})
    assert other is not None
