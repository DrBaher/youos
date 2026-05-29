"""Scheduled email-digest tasks: collect → summarize → deliver one email."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.agent import digest_tasks as dt
from app.db.bootstrap import _migrate_agent_digest_runs
from app.ingestion import gmail_write


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "d.db"
    conn = sqlite3.connect(p)
    _migrate_agent_digest_runs(conn)
    conn.commit()
    conn.close()
    return f"sqlite:///{p}"


def _spec(**over):
    base = dict(name="Newsletters", query="label:Newsletters newer_than:7d",
                schedule="daily", hour=7, deliver_to="", then_archive=False,
                max_messages=50, enabled=True)
    base.update(over)
    return dt.DigestSpec(**base)


def _cfg(enabled=True, send_enabled=True, kill_switch=False):
    return {"enabled": enabled, "send_enabled": send_enabled, "kill_switch": kill_switch}


def _stub_fetch(monkeypatch, items):
    monkeypatch.setattr(dt, "_fetch_for_digest", lambda account, query, limit: list(items))


def _stub_send(monkeypatch):
    calls = []

    def _send(*, account, to, subject, body, backend=None):
        calls.append({"to": to, "subject": subject, "body": body})
        return gmail_write.GmailSendResult(message_id="dg1", raw_response={"id": "dg1"})

    monkeypatch.setattr(gmail_write, "send_email", _send)
    return calls


_ITEMS = [{"id": "m1", "from": "a@x.com", "subject": "Weekly digest", "date": "2026-05-29"},
          {"id": "m2", "from": "b@y.com", "subject": "News", "date": "2026-05-28"}]


# --- validation ------------------------------------------------------------


def test_validate_digest():
    assert dt.validate_digest({"name": "N", "query": "label:X"})[0]
    assert dt.validate_digest({"name": "N", "query": "label:X", "schedule": "weekly",
                               "deliver_to": "me@x.com", "hour": 8})[0]
    assert not dt.validate_digest({"query": "label:X"})[0]                 # no name
    assert not dt.validate_digest({"name": "N"})[0]                        # no query
    assert not dt.validate_digest({"name": "N", "query": "x", "schedule": "hourly"})[0]
    assert not dt.validate_digest({"name": "N", "query": "x", "deliver_to": "nope"})[0]
    assert not dt.validate_digest({"name": "N", "query": "x", "hour": 99})[0]


def test_load_digests_drops_invalid(monkeypatch):
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {"agent": {"digests": {"items": [
        {"name": "Good", "query": "label:A"},
        {"name": "", "query": "label:B"},          # invalid → dropped
        {"name": "Bad", "query": "x", "deliver_to": "junk"},  # invalid → dropped
    ]}}})
    specs = dt.load_digests()
    assert [s.name for s in specs] == ["Good"]


def test_period_key():
    d = datetime(2026, 5, 29, 9, 0, tzinfo=ZoneInfo("UTC"))
    assert dt._period_key("daily", d) == "2026-05-29"
    assert dt._period_key("weekly", d).startswith("2026-W")


# --- body ------------------------------------------------------------------


def test_build_digest_body_uses_model_then_falls_back():
    body = dt.build_digest_body(_ITEMS, complete_fn=lambda p: "SUMMARY HERE")
    assert "SUMMARY HERE" in body and "Weekly digest" in body
    # model error → plain itemised fallback, never empty
    def boom(p):
        raise RuntimeError("model down")
    fb = dt.build_digest_body(_ITEMS, complete_fn=boom)
    assert "Weekly digest" in fb and "News" in fb


# --- claim atomicity -------------------------------------------------------


def test_period_claim_is_atomic(db):
    first = dt._claim_period(db, "Newsletters", "me@x.com", "2026-05-29")
    second = dt._claim_period(db, "Newsletters", "me@x.com", "2026-05-29")
    assert first is not None and second is None          # at-most-once per period
    other = dt._claim_period(db, "Newsletters", "me@x.com", "2026-05-30")
    assert other is not None                              # different period independent


# --- run_digest ------------------------------------------------------------


def test_run_digest_disabled(db, monkeypatch):
    monkeypatch.setattr(dt, "_digest_config", lambda: _cfg(enabled=False))
    calls = _stub_send(monkeypatch)
    assert dt.run_digest(db, "me@x.com", _spec())["status"] == "disabled"
    assert calls == []


def test_dry_run_preview_works_even_when_feature_disabled(db, monkeypatch):
    """Preview is read-only, so it must work before the master flag is on —
    you can preview a digest while it stays fully gated off."""
    monkeypatch.setattr(dt, "_digest_config", lambda: _cfg(enabled=False, send_enabled=False))
    _stub_fetch(monkeypatch, _ITEMS)
    calls = _stub_send(monkeypatch)
    monkeypatch.setattr(dt, "build_digest_body", lambda items, **k: "PREVIEW BODY")
    res = dt.run_digest(db, "me@x.com", _spec(), dry_run=True)
    assert res["status"] == "preview" and res["count"] == 2
    assert calls == [] and dt.list_digest_runs(db) == []


def test_run_digest_dry_run_previews_without_send_or_claim(db, monkeypatch):
    monkeypatch.setattr(dt, "_digest_config", lambda: _cfg())
    _stub_fetch(monkeypatch, _ITEMS)
    calls = _stub_send(monkeypatch)
    monkeypatch.setattr(dt, "build_digest_body", lambda items, **k: "PREVIEW BODY")
    res = dt.run_digest(db, "me@x.com", _spec(), dry_run=True)
    assert res["status"] == "preview" and res["count"] == 2 and res["to"] == "me@x.com"
    assert calls == []                                    # nothing sent
    assert dt.list_digest_runs(db) == []                  # no period row consumed


def test_run_digest_blocked_when_send_gates_closed(db, monkeypatch):
    _stub_fetch(monkeypatch, _ITEMS)
    calls = _stub_send(monkeypatch)
    monkeypatch.setattr(dt, "build_digest_body", lambda items, **k: "B")
    # send disabled
    monkeypatch.setattr(dt, "_digest_config", lambda: _cfg(send_enabled=False))
    r1 = dt.run_digest(db, "me@x.com", _spec(name="A"))
    # kill switch
    monkeypatch.setattr(dt, "_digest_config", lambda: _cfg(kill_switch=True))
    r2 = dt.run_digest(db, "me@x.com", _spec(name="B"))
    assert r1["status"] == "blocked" and r2["status"] == "blocked"
    assert calls == []


def test_run_digest_sends_and_is_at_most_once(db, monkeypatch):
    monkeypatch.setattr(dt, "_digest_config", lambda: _cfg())
    _stub_fetch(monkeypatch, _ITEMS)
    calls = _stub_send(monkeypatch)
    monkeypatch.setattr(dt, "build_digest_body", lambda items, **k: "B")
    now = datetime(2026, 5, 29, 9, 0, tzinfo=ZoneInfo("UTC"))
    first = dt.run_digest(db, "me@x.com", _spec(), now=now)
    second = dt.run_digest(db, "me@x.com", _spec(), now=now)   # same period
    assert first["status"] == "sent" and first["count"] == 2
    assert second["status"] == "skipped_done"
    assert len(calls) == 1 and calls[0]["to"] == "me@x.com"    # sent exactly once
    assert calls[0]["subject"] == "YouOS digest: Newsletters"


def test_run_digest_empty_skips_send_without_burning_period(db, monkeypatch):
    monkeypatch.setattr(dt, "_digest_config", lambda: _cfg())
    calls = _stub_send(monkeypatch)
    monkeypatch.setattr(dt, "build_digest_body", lambda items, **k: "B")
    now = datetime(2026, 5, 29, 9, 0, tzinfo=ZoneInfo("UTC"))
    # 7am: nothing matches yet → empty, no send, and NO period row claimed.
    _stub_fetch(monkeypatch, [])
    r1 = dt.run_digest(db, "me@x.com", _spec(), now=now)
    assert r1["status"] == "empty" and calls == []
    assert dt.list_digest_runs(db) == []                  # period NOT burned
    # later same day: mail arrives → it must still send (period was re-runnable).
    _stub_fetch(monkeypatch, _ITEMS)
    r2 = dt.run_digest(db, "me@x.com", _spec(), now=now)
    assert r2["status"] == "sent" and len(calls) == 1


def test_blocked_gate_does_not_burn_period(db, monkeypatch):
    """Finding 1 regression: a run blocked by a closed send gate must not consume
    the period — opening the gate later in the same period must still send."""
    _stub_fetch(monkeypatch, _ITEMS)
    calls = _stub_send(monkeypatch)
    monkeypatch.setattr(dt, "build_digest_body", lambda items, **k: "B")
    now = datetime(2026, 5, 29, 9, 0, tzinfo=ZoneInfo("UTC"))
    # gate closed → blocked, nothing sent, no period row
    monkeypatch.setattr(dt, "_digest_config", lambda: _cfg(send_enabled=False))
    r1 = dt.run_digest(db, "me@x.com", _spec(), now=now)
    assert r1["status"] == "blocked" and calls == []
    assert dt.list_digest_runs(db) == []
    # operator opens the gate later the same day → it now sends for this period
    monkeypatch.setattr(dt, "_digest_config", lambda: _cfg(send_enabled=True))
    r2 = dt.run_digest(db, "me@x.com", _spec(), now=now)
    assert r2["status"] == "sent" and len(calls) == 1


def test_run_digest_self_heals_missing_table(tmp_path, monkeypatch):
    """Finding 2 regression: a real run on a DB that lacks agent_digest_runs must
    self-heal (ensure_agent_schema) and not crash with 'no such table'."""
    p = tmp_path / "fresh.db"
    import sqlite3
    sqlite3.connect(p).close()                    # empty DB — no agent tables
    db_url = f"sqlite:///{p}"
    monkeypatch.setattr(dt, "_digest_config", lambda: _cfg())
    _stub_fetch(monkeypatch, _ITEMS)
    calls = _stub_send(monkeypatch)
    monkeypatch.setattr(dt, "build_digest_body", lambda items, **k: "B")
    res = dt.run_digest(db_url, "me@x.com", _spec(), now=datetime(2026, 5, 29, 9, 0, tzinfo=ZoneInfo("UTC")))
    assert res["status"] == "sent" and len(calls) == 1


def test_run_digest_then_archive(db, monkeypatch):
    monkeypatch.setattr(dt, "_digest_config", lambda: _cfg())
    _stub_fetch(monkeypatch, _ITEMS)
    _stub_send(monkeypatch)
    monkeypatch.setattr(dt, "build_digest_body", lambda items, **k: "B")
    archived = []
    monkeypatch.setattr(gmail_write, "modify_message_labels",
                        lambda **k: archived.append((k["message_id"], k.get("remove"))) or gmail_write.GmailModifyResult("m", [], ["INBOX"], {}))
    res = dt.run_digest(db, "me@x.com", _spec(then_archive=True),
                        now=datetime(2026, 5, 29, 9, 0, tzinfo=ZoneInfo("UTC")))
    assert res["status"] == "sent" and res["archived"] == 2
    assert [a[0] for a in archived] == ["m1", "m2"]
    assert all(a[1] == ["INBOX"] for a in archived)


def test_run_digest_send_error_recorded(db, monkeypatch):
    monkeypatch.setattr(dt, "_digest_config", lambda: _cfg())
    _stub_fetch(monkeypatch, _ITEMS)
    monkeypatch.setattr(dt, "build_digest_body", lambda items, **k: "B")
    monkeypatch.setattr(gmail_write, "send_email",
                        lambda **k: (_ for _ in ()).throw(gmail_write.GmailWriteError("smtp down")))
    res = dt.run_digest(db, "me@x.com", _spec(), now=datetime(2026, 5, 29, 9, 0, tzinfo=ZoneInfo("UTC")))
    assert res["status"] == "error"
    assert dt.list_digest_runs(db)[0]["status"] == "error"


# --- run_due_digests -------------------------------------------------------


def test_run_due_digests_respects_hour(db, monkeypatch):
    monkeypatch.setattr(dt, "_digest_config", lambda: _cfg())
    monkeypatch.setattr(dt, "load_digests", lambda: [_spec(hour=7)])
    _stub_fetch(monkeypatch, _ITEMS)
    calls = _stub_send(monkeypatch)
    monkeypatch.setattr(dt, "build_digest_body", lambda items, **k: "B")
    tz = ZoneInfo("UTC")
    early = dt.run_due_digests(db, "me@x.com", now=datetime(2026, 5, 29, 6, 0, tzinfo=tz))
    assert early == []                                   # before the digest hour → nothing
    assert calls == []
    later = dt.run_due_digests(db, "me@x.com", now=datetime(2026, 5, 29, 8, 0, tzinfo=tz))
    assert later and later[0]["status"] == "sent"
    assert len(calls) == 1
