"""Tests for autonomous auto-send (Phase B, the policy ladder).

Auto-send is off by default, shadow by default, gated by the delay window,
escalation, per-recipient trust, and the send-frontier gates. These tests never
touch the network.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.agent import send as send_mod
from app.agent import store, triage
from app.db.bootstrap import _migrate_agent_pending_drafts


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """A DB factory. Returns (database_url, insert). Sending gates are open by
    default (send.enabled true, kill-switch off); tests set auto_send config."""
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    _migrate_agent_pending_drafts(conn)
    conn.commit()  # release the migration's write lock before other connections
    seq = {"n": 0}

    def insert(
        *, sender_email="a@x.com", quality_score=0.9, needs_reply_score=0.95,
        subject="Coffee", body="Thursday at 2 work?", send_state="draft_created",
        status="sent", pushed_minutes_ago=120,
    ):
        seq["n"] += 1
        n = seq["n"]
        conn.execute(
            "INSERT INTO agent_pending_drafts "
            "(message_id, thread_id, account, sender_email, subject, body, "
            " needs_reply_score, reasons_json, cold_outreach, tier, draft, "
            " status, gmail_draft_id, send_state, quality_score, sent_at) "
            "VALUES (?, ?, 'me@x.com', ?, ?, ?, ?, '[]', 0, 'draft', 'Hi', ?, ?, ?, ?, "
            " datetime('now', ?))",
            (f"m{n}", f"t{n}", sender_email, subject, body, needs_reply_score,
             status, f"gd_{n}", send_state, quality_score, f"-{pushed_minutes_ago} minutes"),
        )
        conn.commit()
        return n

    monkeypatch.setattr(send_mod, "_send_config", lambda: {"enabled": True, "kill_switch": False})
    return f"sqlite:///{db}", insert


def _trust_rows(database_url, sender_email, n):
    """Seed n CONFIRMED-sent rows to give a recipient trust >= n. Trust counts
    confirmed sends only — status='sent' with send_state NULL is the manual
    mark_sent path (the user sent it themselves)."""
    conn = sqlite3.connect(database_url.removeprefix("sqlite:///"))
    for i in range(n):
        conn.execute(
            "INSERT INTO agent_pending_drafts "
            "(message_id, thread_id, account, sender_email, needs_reply_score, "
            " reasons_json, cold_outreach, tier, draft, status, send_state) "
            "VALUES (?, ?, 'me@x.com', ?, 0.9, '[]', 0, 'draft', 'x', 'sent', NULL)",
            (f"trust{sender_email}{i}", f"tt{i}", sender_email),
        )
    conn.commit()
    conn.close()


# --- gating ----------------------------------------------------------------


def test_auto_send_noop_when_disabled(seeded, monkeypatch):
    database_url, insert = seeded
    insert()
    monkeypatch.setattr(triage, "_auto_send_config", lambda: {
        "enabled": False, "mode": "shadow", "delay_minutes": 60,
        "min_recipient_trust": 0, "max_per_sweep": 5, "daily_send_cap": 5,
    })
    out = triage._maybe_auto_send(database_url=database_url, account="me@x.com")
    assert out == []


def test_recent_draft_inside_delay_window_is_not_sent(seeded, monkeypatch):
    database_url, insert = seeded
    insert(pushed_minutes_ago=5)  # pushed 5 min ago, delay is 60
    _trust_rows(database_url, "a@x.com", 3)
    monkeypatch.setattr(triage, "_auto_send_config", lambda: {
        "enabled": True, "mode": "shadow", "delay_minutes": 60,
        "min_recipient_trust": 0, "max_per_sweep": 5, "daily_send_cap": 5,
    })
    out = triage._maybe_auto_send(database_url=database_url, account="me@x.com")
    assert out == []  # not yet due


def test_low_trust_recipient_held(seeded, monkeypatch):
    database_url, insert = seeded
    rid = insert(sender_email="stranger@x.com")  # no trust history
    monkeypatch.setattr(triage, "_auto_send_config", lambda: {
        "enabled": True, "mode": "shadow", "delay_minutes": 60,
        "min_recipient_trust": 3, "max_per_sweep": 5, "daily_send_cap": 5,
    })
    out = triage._maybe_auto_send(database_url=database_url, account="me@x.com")
    assert len(out) == 1
    assert out[0]["id"] == rid
    assert out[0]["action"] == "held"
    assert "trust" in out[0]["reason"]


def test_high_stakes_draft_held(seeded, monkeypatch):
    database_url, insert = seeded
    rid = insert(subject="Invoice", body="Payment of $500 is due.")
    _trust_rows(database_url, "a@x.com", 5)
    monkeypatch.setattr(triage, "_auto_send_config", lambda: {
        "enabled": True, "mode": "shadow", "delay_minutes": 60,
        "min_recipient_trust": 3, "max_per_sweep": 5, "daily_send_cap": 5,
    })
    out = triage._maybe_auto_send(database_url=database_url, account="me@x.com")
    assert out[0]["id"] == rid
    assert out[0]["action"] == "held"
    assert out[0]["reason"] == "ask"  # escalation routed it to a human


# --- shadow vs live --------------------------------------------------------


def test_shadow_send_records_shadow_without_network(seeded, monkeypatch):
    database_url, insert = seeded
    rid = insert()
    _trust_rows(database_url, "a@x.com", 3)
    monkeypatch.setattr(triage, "_auto_send_config", lambda: {
        "enabled": True, "mode": "shadow", "delay_minutes": 60,
        "min_recipient_trust": 3, "max_per_sweep": 5, "daily_send_cap": 5,
    })
    from app.ingestion import gmail_write
    monkeypatch.setattr(
        gmail_write, "send_draft",
        lambda **kw: (_ for _ in ()).throw(AssertionError("shadow must not send")),
    )
    out = triage._maybe_auto_send(database_url=database_url, account="me@x.com")
    assert out[0]["action"] == "shadow"
    assert store.get(database_url, rid)["send_state"] == "shadow"


def test_live_send_actually_sends(seeded, monkeypatch):
    database_url, insert = seeded
    rid = insert()
    _trust_rows(database_url, "a@x.com", 3)
    monkeypatch.setattr(triage, "_auto_send_config", lambda: {
        "enabled": True, "mode": "live", "delay_minutes": 60,
        "min_recipient_trust": 3, "max_per_sweep": 5, "daily_send_cap": 5,
    })
    from app.ingestion import gmail_write
    monkeypatch.setattr(
        gmail_write, "send_draft",
        lambda **kw: gmail_write.GmailSendResult(message_id="sent_1", raw_response={}),
    )
    out = triage._maybe_auto_send(database_url=database_url, account="me@x.com")
    assert out[0]["action"] == "sent"
    row = store.get(database_url, rid)
    assert row["send_state"] == "sent"
    assert row["sent_message_id"] == "sent_1"


def test_live_send_blocked_by_kill_switch(seeded, monkeypatch):
    database_url, insert = seeded
    rid = insert()
    _trust_rows(database_url, "a@x.com", 3)
    monkeypatch.setattr(triage, "_auto_send_config", lambda: {
        "enabled": True, "mode": "live", "delay_minutes": 60,
        "min_recipient_trust": 3, "max_per_sweep": 5, "daily_send_cap": 5,
    })
    # Kill-switch on → send path refuses.
    monkeypatch.setattr(send_mod, "_send_config", lambda: {"enabled": True, "kill_switch": True})
    out = triage._maybe_auto_send(database_url=database_url, account="me@x.com")
    assert out[0]["action"] == "error"
    assert store.get(database_url, rid)["send_state"] == "draft_created"


# --- store helpers ---------------------------------------------------------


def test_recipient_trust_counts_kept_only(seeded):
    database_url, insert = seeded
    _trust_rows(database_url, "vip@x.com", 4)
    # A dismissed row to the same recipient must not count.
    conn = sqlite3.connect(database_url.removeprefix("sqlite:///"))
    conn.execute(
        "INSERT INTO agent_pending_drafts "
        "(message_id, thread_id, account, sender_email, needs_reply_score, "
        " reasons_json, cold_outreach, tier, status) "
        "VALUES ('d1','d1','me@x.com','vip@x.com',0.9,'[]',0,'draft','dismissed')"
    )
    conn.commit()
    conn.close()
    assert store.recipient_trust(database_url, "vip@x.com") == 4


def test_due_for_auto_send_respects_window_and_state(seeded):
    database_url, insert = seeded
    due_id = insert(pushed_minutes_ago=120)
    insert(pushed_minutes_ago=5)            # too recent
    insert(send_state="sent", pushed_minutes_ago=200)  # already sent
    due = store.due_for_auto_send(database_url, account="me@x.com", delay_minutes=60)
    assert [r["id"] for r in due] == [due_id]


def test_daily_send_cap_holds_excess(seeded, monkeypatch):
    database_url, insert = seeded
    insert(sender_email="vip@x.com")
    insert(sender_email="vip@x.com")
    _trust_rows(database_url, "vip@x.com", 3)
    monkeypatch.setattr(triage, "_auto_send_config", lambda: {
        "enabled": True, "mode": "live", "delay_minutes": 60,
        "min_recipient_trust": 0, "max_per_sweep": 5, "daily_send_cap": 1,
    })
    from app.ingestion import gmail_write
    monkeypatch.setattr(
        gmail_write, "send_draft",
        lambda **kw: gmail_write.GmailSendResult(message_id="m", raw_response={}),
    )
    out = triage._maybe_auto_send(database_url=database_url, account="me@x.com")
    sent = [r for r in out if r["action"] == "sent"]
    capped = [r for r in out if r["action"] == "held" and "cap" in r.get("reason", "")]
    assert len(sent) == 1
    assert len(capped) == 1


def test_high_stakes_draft_text_is_held(seeded, monkeypatch):
    """Even when the inbound is benign, a draft that itself states money/legal
    content is held — escalation must scan the draft, not just the inbound."""
    database_url, insert = seeded
    insert(subject="Re: hello", body="Just saying hi!",
           # the DRAFT carries the high-stakes content the inbound lacks
           )
    # Overwrite the seeded draft with high-stakes text.
    conn = sqlite3.connect(database_url.removeprefix("sqlite:///"))
    conn.execute("UPDATE agent_pending_drafts SET draft = 'I will wire you the $5,000 deposit today.'")
    conn.commit()
    conn.close()
    _trust_rows(database_url, "a@x.com", 3)
    monkeypatch.setattr(triage, "_auto_send_config", lambda: {
        "enabled": True, "mode": "live", "delay_minutes": 60,
        "min_recipient_trust": 0, "max_per_sweep": 5, "daily_send_cap": 5,
    })
    from app.ingestion import gmail_write
    monkeypatch.setattr(
        gmail_write, "send_draft",
        lambda **kw: (_ for _ in ()).throw(AssertionError("high-stakes draft must not send")),
    )
    out = triage._maybe_auto_send(database_url=database_url, account="me@x.com")
    assert out[0]["action"] == "held"
    assert "high-stakes draft" in out[0]["reason"]


def test_dismiss_between_select_and_claim_is_not_sent(seeded, monkeypatch):
    """TOCTOU guard: a row dismissed after begin_send's pre-read must not send."""
    database_url, insert = seeded
    rid = insert()
    # Simulate the dismiss landing right before the atomic claim.
    conn = sqlite3.connect(database_url.removeprefix("sqlite:///"))
    conn.execute("UPDATE agent_pending_drafts SET status = 'dismissed' WHERE id = ?", (rid,))
    conn.commit()
    conn.close()
    state, _ = store.begin_send(database_url, rid)
    assert state == "dismissed"


def test_daily_send_cap_zero_disables_auto_send(seeded, monkeypatch):
    """daily_send_cap <= 0 DISABLES auto-send (mirrors the auto-push cap), not
    'unlimited'."""
    database_url, insert = seeded
    insert()
    _trust_rows(database_url, "a@x.com", 3)
    monkeypatch.setattr(triage, "_auto_send_config", lambda: {
        "enabled": True, "mode": "live", "delay_minutes": 60,
        "min_recipient_trust": 0, "max_per_sweep": 5, "daily_send_cap": 0,
    })
    from app.ingestion import gmail_write
    monkeypatch.setattr(
        gmail_write, "send_draft",
        lambda **kw: (_ for _ in ()).throw(AssertionError("cap=0 must disable sending")),
    )
    assert triage._maybe_auto_send(database_url=database_url, account="me@x.com") == []


def test_held_row_excluded_from_due_for_auto_send(seeded):
    """A persisted hold row is never returned for auto-send, even if pushed and
    past the delay window."""
    database_url, insert = seeded
    normal = insert()
    held = insert()
    conn = sqlite3.connect(database_url.removeprefix("sqlite:///"))
    conn.execute("UPDATE agent_pending_drafts SET hold = 1 WHERE id = ?", (held,))
    conn.commit()
    conn.close()
    due_ids = [r["id"] for r in store.due_for_auto_send(database_url, account="me@x.com", delay_minutes=60)]
    assert normal in due_ids
    assert held not in due_ids


def test_amended_draft_high_stakes_is_caught(seeded, monkeypatch):
    """The veto scans the body that's actually sent (amended_draft) — a benign
    original draft with a money-inventing amendment is held."""
    database_url, insert = seeded
    rid = insert(body="just saying hi", subject="hi")
    conn = sqlite3.connect(database_url.removeprefix("sqlite:///"))
    conn.execute(
        "UPDATE agent_pending_drafts SET draft = 'Sounds good!', "
        "amended_draft = 'I will wire the $5,000 deposit today.' WHERE id = ?", (rid,))
    conn.commit()
    conn.close()
    _trust_rows(database_url, "a@x.com", 3)
    monkeypatch.setattr(triage, "_auto_send_config", lambda: {
        "enabled": True, "mode": "live", "delay_minutes": 60,
        "min_recipient_trust": 0, "max_per_sweep": 5, "daily_send_cap": 5,
    })
    from app.ingestion import gmail_write
    monkeypatch.setattr(
        gmail_write, "send_draft",
        lambda **kw: (_ for _ in ()).throw(AssertionError("high-stakes amended draft must not send")),
    )
    out = triage._maybe_auto_send(database_url=database_url, account="me@x.com")
    assert out[0]["action"] == "held"
    assert "high-stakes draft" in out[0]["reason"]


def test_reaper_frees_stale_sending_rows(seeded):
    database_url, insert = seeded
    rid = insert()
    conn = sqlite3.connect(database_url.removeprefix("sqlite:///"))
    conn.execute(
        "UPDATE agent_pending_drafts SET send_state = 'sending', "
        "updated_at = datetime('now', '-30 minutes') WHERE id = ?", (rid,))
    conn.commit()
    conn.close()
    assert store.reap_stale_sending(database_url, older_than_minutes=10) == 1
    assert store.get(database_url, rid)["send_state"] == "draft_created"
    # A fresh 'sending' row (just claimed) must NOT be reaped.
    store.begin_send(database_url, rid)
    assert store.reap_stale_sending(database_url, older_than_minutes=10) == 0


def test_run_triage_wires_auto_send_and_respects_delay(tmp_path, monkeypatch):
    """End-to-end through run_triage: an OLD pushed+trusted draft auto-sends
    (shadow), while the draft created in THIS sweep does not (delay window)."""
    from app.agent.inbox_fetch import InboxMessage
    from app.db.bootstrap import _migrate_agent_audit, _migrate_agent_pending_drafts

    db = tmp_path / "rt.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY, inbound_author TEXT)")
    _migrate_agent_pending_drafts(conn)
    _migrate_agent_audit(conn)
    # An OLD pushed draft to a trusted recipient, eligible for auto-send.
    conn.execute(
        "INSERT INTO agent_pending_drafts "
        "(message_id, thread_id, account, sender_email, subject, body, "
        " needs_reply_score, reasons_json, cold_outreach, tier, draft, status, "
        " gmail_draft_id, send_state, quality_score, sent_at) "
        "VALUES ('old','told','you@example.com','vip@partner.com','Coffee','Thursday?',"
        " 0.95,'[]',0,'draft','Sure, Thursday works.','sent','gd_old','draft_created',0.9,"
        " datetime('now','-120 minutes'))"
    )
    # Trust for vip@partner.com (3 manual-sent rows).
    for i in range(3):
        conn.execute(
            "INSERT INTO agent_pending_drafts "
            "(message_id, thread_id, account, sender_email, needs_reply_score, "
            " reasons_json, cold_outreach, tier, draft, status, send_state) "
            "VALUES (?,?, 'you@example.com','vip@partner.com',0.9,'[]',0,'draft','x','sent',NULL)",
            (f"tr{i}", f"ttr{i}"),
        )
    conn.commit()
    conn.close()
    db_url = f"sqlite:///{db}"

    new_msg = InboxMessage(
        message_id="new1", thread_id="tnew", account="you@example.com",
        sender="Bob <bob@new.com>", sender_email="bob@new.com",
        subject="Quick q", body="Could you confirm the time?", headers={},
    )
    monkeypatch.setattr("app.agent.triage.fetch_unread", lambda *a, **k: [new_msg])

    class _Resp:
        draft = "Yes, that works."
        model_used = "qwen2.5-1.5b-lora"
        repairs: list[str] = []
        quality_score = 0.9

    monkeypatch.setattr("app.generation.service.generate_draft", lambda req, **kw: _Resp())
    monkeypatch.setattr(triage, "_auto_send_config", lambda: {
        "enabled": True, "mode": "shadow", "delay_minutes": 60,
        "min_recipient_trust": 3, "max_per_sweep": 5, "daily_send_cap": 5,
    })
    monkeypatch.setattr(send_mod, "_send_config", lambda: {"enabled": True, "kill_switch": False})

    result = triage.run_triage(
        account="you@example.com", database_url=db_url, configs_dir=tmp_path,
    )
    actions = {(a["id"], a["action"]) for a in result.auto_sent}
    # The OLD eligible row was shadow-sent...
    assert any(a == "shadow" for _, a in actions), result.auto_sent
    # ...and nothing else (the new sweep draft has no Gmail draft + is too recent).
    assert all(a == "shadow" for _, a in actions)
    assert store.get(db_url, 1)["send_state"] == "shadow"
