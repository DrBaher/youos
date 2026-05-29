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
    """Seed n kept (amended) rows to give a recipient trust >= n."""
    conn = sqlite3.connect(database_url.removeprefix("sqlite:///"))
    for i in range(n):
        conn.execute(
            "INSERT INTO agent_pending_drafts "
            "(message_id, thread_id, account, sender_email, needs_reply_score, "
            " reasons_json, cold_outreach, tier, draft, status) "
            "VALUES (?, ?, 'me@x.com', ?, 0.9, '[]', 0, 'draft', 'x', 'amended')",
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
        "min_recipient_trust": 0, "max_per_sweep": 5,
    })
    out = triage._maybe_auto_send(database_url=database_url, account="me@x.com")
    assert out == []


def test_recent_draft_inside_delay_window_is_not_sent(seeded, monkeypatch):
    database_url, insert = seeded
    insert(pushed_minutes_ago=5)  # pushed 5 min ago, delay is 60
    _trust_rows(database_url, "a@x.com", 3)
    monkeypatch.setattr(triage, "_auto_send_config", lambda: {
        "enabled": True, "mode": "shadow", "delay_minutes": 60,
        "min_recipient_trust": 0, "max_per_sweep": 5,
    })
    out = triage._maybe_auto_send(database_url=database_url, account="me@x.com")
    assert out == []  # not yet due


def test_low_trust_recipient_held(seeded, monkeypatch):
    database_url, insert = seeded
    rid = insert(sender_email="stranger@x.com")  # no trust history
    monkeypatch.setattr(triage, "_auto_send_config", lambda: {
        "enabled": True, "mode": "shadow", "delay_minutes": 60,
        "min_recipient_trust": 3, "max_per_sweep": 5,
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
        "min_recipient_trust": 3, "max_per_sweep": 5,
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
        "min_recipient_trust": 3, "max_per_sweep": 5,
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
        "min_recipient_trust": 3, "max_per_sweep": 5,
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
        "min_recipient_trust": 3, "max_per_sweep": 5,
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
