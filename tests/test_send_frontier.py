"""Tests for the send frontier (Phase B).

Sending is hard-gated: disabled by default, blocked by the kill-switch, and
valid only on a row that already has a Gmail draft. These tests never touch the
network — the backend ``send_draft`` and ``subprocess.run`` are mocked.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.agent import send as send_mod
from app.agent import store
from app.db.bootstrap import _migrate_agent_pending_drafts
from app.ingestion import gmail_write


@pytest.fixture
def pushed_row(tmp_path, monkeypatch):
    """A DB with one pushed row (has a Gmail draft, send_state='draft_created').
    Returns (database_url, row_id). Sending is enabled by default in the
    fixture; individual tests override the gates as needed."""
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    _migrate_agent_pending_drafts(conn)
    conn.execute(
        "INSERT INTO agent_pending_drafts "
        "(message_id, thread_id, account, sender_email, needs_reply_score, "
        " reasons_json, cold_outreach, tier, draft, status, gmail_draft_id, send_state) "
        "VALUES ('m1','t1','me@x.com','a@partner.com',0.9,'[]',0,'draft',"
        "'Hi','sent','gd_1','draft_created')"
    )
    conn.commit()
    conn.close()
    database_url = f"sqlite:///{db}"

    # Default: sending enabled, kill-switch off. Tests override per-case.
    monkeypatch.setattr(send_mod, "_send_config", lambda: {"enabled": True, "kill_switch": False})
    return database_url, 1


# --- gating ----------------------------------------------------------------


def test_send_blocked_when_disabled(pushed_row, monkeypatch):
    database_url, row_id = pushed_row
    monkeypatch.setattr(send_mod, "_send_config", lambda: {"enabled": False, "kill_switch": False})
    monkeypatch.setattr(
        gmail_write, "send_draft",
        lambda **kw: (_ for _ in ()).throw(AssertionError("must not send when disabled")),
    )
    out = send_mod.send_pending_row(database_url, row_id)
    assert not out.ok
    assert out.http_status == 403
    assert store.get(database_url, row_id)["send_state"] == "draft_created"


def test_kill_switch_blocks_even_when_enabled(pushed_row, monkeypatch):
    database_url, row_id = pushed_row
    monkeypatch.setattr(send_mod, "_send_config", lambda: {"enabled": True, "kill_switch": True})
    monkeypatch.setattr(
        gmail_write, "send_draft",
        lambda **kw: (_ for _ in ()).throw(AssertionError("kill-switch must block")),
    )
    out = send_mod.send_pending_row(database_url, row_id)
    assert not out.ok
    assert out.http_status == 403
    assert "kill-switch" in out.detail


def test_kill_switch_blocks_shadow_too(pushed_row, monkeypatch):
    database_url, row_id = pushed_row
    monkeypatch.setattr(send_mod, "_send_config", lambda: {"enabled": True, "kill_switch": True})
    out = send_mod.send_pending_row(database_url, row_id, shadow=True)
    assert not out.ok
    assert out.http_status == 403


# --- shadow send -----------------------------------------------------------


def test_shadow_send_works_even_when_disabled(pushed_row, monkeypatch):
    database_url, row_id = pushed_row
    monkeypatch.setattr(send_mod, "_send_config", lambda: {"enabled": False, "kill_switch": False})
    monkeypatch.setattr(
        gmail_write, "send_draft",
        lambda **kw: (_ for _ in ()).throw(AssertionError("shadow must not touch Gmail")),
    )
    out = send_mod.send_pending_row(database_url, row_id, shadow=True)
    assert out.ok and out.shadow
    assert store.get(database_url, row_id)["send_state"] == "shadow"


# --- real send -------------------------------------------------------------


def test_real_send_when_enabled(pushed_row, monkeypatch):
    database_url, row_id = pushed_row
    seen = {}
    monkeypatch.setattr(
        gmail_write, "send_draft",
        lambda **kw: seen.update(kw) or gmail_write.GmailSendResult(message_id="msg_99", raw_response={"id": "msg_99"}),
    )
    out = send_mod.send_pending_row(database_url, row_id)
    assert out.ok and not out.shadow
    assert out.sent_message_id == "msg_99"
    assert seen["draft_id"] == "gd_1"
    assert seen["account"] == "me@x.com"
    row = store.get(database_url, row_id)
    assert row["send_state"] == "sent"
    assert row["sent_message_id"] == "msg_99"
    assert row["actually_sent_at"]


def test_send_requires_a_pushed_draft(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    _migrate_agent_pending_drafts(conn)
    conn.execute(
        "INSERT INTO agent_pending_drafts "
        "(message_id, thread_id, account, sender_email, needs_reply_score, "
        " reasons_json, cold_outreach, tier, draft, status) "
        "VALUES ('m2','t2','me@x.com','a@x.com',0.9,'[]',0,'draft','Hi','pending')"
    )
    conn.commit()
    conn.close()
    database_url = f"sqlite:///{db}"
    monkeypatch.setattr(send_mod, "_send_config", lambda: {"enabled": True, "kill_switch": False})
    out = send_mod.send_pending_row(database_url, 1)
    assert not out.ok
    assert out.http_status == 409


def test_send_is_idempotent(pushed_row, monkeypatch):
    database_url, row_id = pushed_row
    calls = {"n": 0}

    def _send(**kw):
        calls["n"] += 1
        return gmail_write.GmailSendResult(message_id="msg_1", raw_response={})

    monkeypatch.setattr(gmail_write, "send_draft", _send)
    first = send_mod.send_pending_row(database_url, row_id)
    second = send_mod.send_pending_row(database_url, row_id)
    assert first.ok and second.ok
    assert second.sent_already
    assert calls["n"] == 1  # only sent once


def test_backend_error_rolls_back_to_draft_created(pushed_row, monkeypatch):
    database_url, row_id = pushed_row

    def _boom(**kw):
        raise gmail_write.GmailWriteError("smtp exploded")

    monkeypatch.setattr(gmail_write, "send_draft", _boom)
    out = send_mod.send_pending_row(database_url, row_id)
    assert not out.ok
    assert out.http_status == 502
    # Claim rolled back so the user can retry.
    assert store.get(database_url, row_id)["send_state"] == "draft_created"


# --- store state machine ---------------------------------------------------


def test_begin_send_claims_then_finalize(pushed_row):
    database_url, row_id = pushed_row
    state, draft_id = store.begin_send(database_url, row_id)
    assert state == "claimed"
    assert draft_id == "gd_1"
    assert store.get(database_url, row_id)["send_state"] == "sending"
    store.finalize_send(database_url, row_id, sent_message_id="m9")
    assert store.get(database_url, row_id)["send_state"] == "sent"


def test_begin_send_race_lost_when_already_sending(pushed_row):
    database_url, row_id = pushed_row
    store.begin_send(database_url, row_id)  # first claim
    state, _ = store.begin_send(database_url, row_id)  # second
    assert state == "race_lost"


# --- backend command shape -------------------------------------------------


def test_gog_send_builds_verified_command(monkeypatch):
    captured = {}

    class _Result:
        returncode = 0
        stdout = '{"id": "msg_sent_1", "threadId": "t1"}'
        stderr = ""

    def _run(cmd, **kw):
        captured["cmd"] = cmd
        return _Result()

    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", _run)
    monkeypatch.setattr(
        "app.core.config.get_ingestion_google_backend", lambda: "gog"
    )
    res = gmail_write.send_draft(account="me@x.com", draft_id="gd_42")
    assert res.message_id == "msg_sent_1"
    cmd = captured["cmd"]
    assert cmd[:5] == ["gog", "gmail", "drafts", "send", "gd_42"]
    assert "--account" in cmd and "me@x.com" in cmd
    assert "--force" in cmd and "--no-input" in cmd and "--json" in cmd
    assert "--dry-run" not in cmd


def test_gog_send_dry_run_passes_flag(monkeypatch):
    captured = {}

    class _Result:
        returncode = 0
        stdout = "{}"
        stderr = ""

    monkeypatch.setattr(
        "app.ingestion.gmail_write.subprocess.run",
        lambda cmd, **kw: captured.update(cmd=cmd) or _Result(),
    )
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gog")
    gmail_write.send_draft(account="me@x.com", draft_id="gd_42", dry_run=True)
    assert "--dry-run" in captured["cmd"]
