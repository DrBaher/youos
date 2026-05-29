"""Scheduled email-digest tasks: collect → summarize → deliver one email."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.agent import digest_tasks as dt
from app.db.bootstrap import _migrate_agent_digest_items, _migrate_agent_digest_runs
from app.ingestion import gmail_write


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "d.db"
    conn = sqlite3.connect(p)
    _migrate_agent_digest_runs(conn)
    _migrate_agent_digest_items(conn)
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


def test_save_digests_round_trips_and_preserves_master_flag(tmp_path, monkeypatch):
    import app.core.config as config_mod

    cfg = tmp_path / "youos_config.yaml"
    cfg.write_text("agent:\n  digests:\n    enabled: true\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    config_mod.load_config.cache_clear()
    try:
        dt.save_digests([{"name": "N", "query": "label:X", "schedule": "weekly", "weekday": "friday", "hour": 17}])
        loaded = dt.load_digests()
        assert len(loaded) == 1 and loaded[0].name == "N"
        assert loaded[0].weekday == 4 and loaded[0].schedule == "weekly" and loaded[0].hour == 17
        # the master flag (agent.digests.enabled) must be preserved across the write
        assert (config_mod.load_config() or {})["agent"]["digests"]["enabled"] is True
    finally:
        config_mod.load_config.cache_clear()


def test_save_digests_rejects_invalid(tmp_path, monkeypatch):
    import app.core.config as config_mod

    cfg = tmp_path / "youos_config.yaml"
    cfg.write_text("user: {}\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    config_mod.load_config.cache_clear()
    try:
        with pytest.raises(ValueError):
            dt.save_digests([{"name": "", "query": "x"}])   # no name
    finally:
        config_mod.load_config.cache_clear()


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


def test_parse_weekday():
    assert dt._parse_weekday("monday") == 0 and dt._parse_weekday("Fri") == 4
    assert dt._parse_weekday(6) == 6 and dt._parse_weekday("3") == 3
    assert dt._parse_weekday("noneday") is None and dt._parse_weekday(9) is None
    assert dt._parse_weekday(True) is None  # bool is not a weekday


def test_validate_weekday_and_minute():
    assert dt.validate_digest({"name": "N", "query": "x", "schedule": "weekly",
                               "weekday": "friday", "hour": 8, "minute": 30})[0]
    assert not dt.validate_digest({"name": "N", "query": "x", "weekday": "funday"})[0]
    assert not dt.validate_digest({"name": "N", "query": "x", "minute": 75})[0]


def test_daily_due_is_bounded_catch_up():
    tz = ZoneInfo("UTC")
    spec = _spec(schedule="daily", hour=7, minute=30)
    at = lambda h, m: datetime(2026, 5, 29, h, m, tzinfo=tz)  # noqa: E731
    assert dt._is_due(spec, at(7, 30)) is True           # exactly the time
    assert dt._is_due(spec, at(9, 0)) is True             # within the 3h catch-up window
    assert dt._is_due(spec, at(7, 29)) is False           # one minute early
    # BOUNDED: enabling/ticking long after the time does NOT fire (the b120 fix —
    # this is what stops an evening enable from blasting a morning digest).
    assert dt._is_due(spec, at(11, 0)) is False           # 3.5h after → outside catch-up
    assert dt._is_due(spec, at(22, 0)) is False


def test_weekly_due_is_weekday_exact_and_bounded():
    tz = ZoneInfo("UTC")
    # Friday = weekday 4. 2026-05-29 is a Friday.
    spec = _spec(schedule="weekly", weekday=4, hour=17, minute=0)
    assert datetime(2026, 5, 29, 17, 0, tzinfo=tz).weekday() == 4   # sanity: Friday
    assert dt._is_due(spec, datetime(2026, 5, 29, 17, 0, tzinfo=tz)) is True   # Fri 17:00
    assert dt._is_due(spec, datetime(2026, 5, 29, 16, 59, tzinfo=tz)) is False # too early
    assert dt._is_due(spec, datetime(2026, 5, 29, 22, 0, tzinfo=tz)) is False  # 5h later → outside catch-up
    assert dt._is_due(spec, datetime(2026, 5, 27, 17, 0, tzinfo=tz)) is False  # Wed (wrong day)
    assert dt._is_due(spec, datetime(2026, 5, 30, 1, 0, tzinfo=tz)) is False   # Sat (NOT its weekday)


def test_run_due_digests_account_scoping(db, monkeypatch):
    monkeypatch.setattr(dt, "_digest_config", lambda: _cfg())
    monkeypatch.setattr(dt, "load_digests", lambda: [_spec(account="only@x.com", hour=7)])
    _stub_fetch(monkeypatch, _ITEMS)
    calls = _stub_send(monkeypatch)
    monkeypatch.setattr(dt, "build_digest_body", lambda items, **k: "B")
    at8 = datetime(2026, 5, 29, 8, 0, tzinfo=ZoneInfo("UTC"))
    # scheduler tick for a DIFFERENT account → digest is skipped (not its account)
    assert dt.run_due_digests(db, "other@y.com", now=at8) == [] and calls == []
    # tick for the digest's own account → it runs
    out = dt.run_due_digests(db, "only@x.com", now=at8)
    assert out and out[0]["status"] == "sent" and len(calls) == 1


def test_validate_account():
    assert dt.validate_digest({"name": "N", "query": "x", "account": "me@x.com"})[0]
    assert dt.validate_digest({"name": "N", "query": "x", "account": ""})[0]
    assert not dt.validate_digest({"name": "N", "query": "x", "account": "not-an-email"})[0]


# --- body ------------------------------------------------------------------


def test_custom_prompt_drives_the_summary():
    seen = {}
    def cap(p):
        seen["p"] = p
        return "SUMMARY"
    # custom prompt is used as the instruction
    dt.build_digest_body(_ITEMS, prompt="Make a haiku of my inbox.", complete_fn=cap)
    assert "Make a haiku of my inbox." in seen["p"]
    assert "Weekly digest" in seen["p"]            # items still appended as source
    # blank prompt → the default instruction
    dt.build_digest_body(_ITEMS, prompt="", complete_fn=cap)
    assert "Worth attention:" in seen["p"] or "ONE short bullet" in seen["p"]


def test_validate_and_normalize_prompt():
    assert dt.validate_digest({"name": "N", "query": "x", "prompt": "focus on deadlines"})[0]
    assert not dt.validate_digest({"name": "N", "query": "x", "prompt": "z" * 5000})[0]   # too long
    assert dt._normalize_digest({"name": "N", "query": "x", "prompt": "  hi  "}).prompt == "hi"


def test_build_digest_body_uses_model_then_falls_back():
    body = dt.build_digest_body(_ITEMS, complete_fn=lambda p: "SUMMARY HERE")
    assert "SUMMARY HERE" in body and "Weekly digest" in body
    # model error → plain itemised fallback, never empty
    def boom(p):
        raise RuntimeError("model down")
    fb = dt.build_digest_body(_ITEMS, complete_fn=boom)
    assert "Weekly digest" in fb and "News" in fb


def test_summary_model_validation_and_selection(monkeypatch):
    assert dt.validate_digest({"name": "N", "query": "x", "summary_model": "local"})[0]
    assert dt.validate_digest({"name": "N", "query": "x", "summary_model": "cloud"})[0]
    assert not dt.validate_digest({"name": "N", "query": "x", "summary_model": "gpt5"})[0]
    # 'cloud' routes to the Claude CLI helper
    import app.generation.service as gen
    monkeypatch.setattr(gen, "_call_claude_cli", lambda p, **k: "CLOUD SUMMARY")
    body = dt.build_digest_body(_ITEMS, model="cloud")
    assert "CLOUD SUMMARY" in body
    # 'local' routes to the warm model server
    import app.core.model_server as ms
    monkeypatch.setattr(ms, "is_enabled", lambda: True)
    monkeypatch.setattr(ms, "complete", lambda p, **k: "LOCAL SUMMARY")
    assert "LOCAL SUMMARY" in dt.build_digest_body(_ITEMS, model="local")
    # default spec uses local
    assert dt._normalize_digest({"name": "N", "query": "x"}).summary_model == "local"


def test_fetch_passes_max_so_the_cap_applies(monkeypatch):
    """Regression: gog search defaults to --max=10, so the configured cap must be
    passed explicitly or a 50-message digest silently only ever sees 10."""
    seen = {}

    class _R:
        returncode = 0
        stdout = "[]"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: seen.update(cmd=cmd) or _R())
    dt._fetch_for_digest("me@x.com", "label:X", 50)
    cmd = seen["cmd"]
    assert "--max" in cmd and cmd[cmd.index("--max") + 1] == "50"


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


def test_undigested_filters_already_sent(db):
    dt._record_digested(db, "Newsletters", "me@x.com", ["m1"], "2026-05-29")
    out = dt._undigested(db, "Newsletters", "me@x.com", _ITEMS)   # _ITEMS = m1, m2
    assert [it["id"] for it in out] == ["m2"]                      # m1 filtered, m2 kept
    # dedup is scoped per digest NAME — a different digest isn't affected
    assert [it["id"] for it in dt._undigested(db, "Other", "me@x.com", _ITEMS)] == ["m1", "m2"]


def test_digest_does_not_repeat_messages_across_runs(db, monkeypatch):
    monkeypatch.setattr(dt, "_digest_config", lambda: _cfg())
    monkeypatch.setattr(dt, "build_digest_body", lambda items, **k: "B")
    calls = _stub_send(monkeypatch)
    d1 = datetime(2026, 5, 29, 9, 0, tzinfo=ZoneInfo("UTC"))
    d2 = datetime(2026, 5, 30, 9, 0, tzinfo=ZoneInfo("UTC"))   # next day → new period

    _stub_fetch(monkeypatch, _ITEMS)                            # day 1: m1, m2
    r1 = dt.run_digest(db, "me@x.com", _spec(), now=d1)
    assert r1["status"] == "sent" and r1["count"] == 2

    # day 2: m1, m2 still match the query PLUS a new m3 → only m3 is sent
    _stub_fetch(monkeypatch, _ITEMS + [{"id": "m3", "from": "c@z.com", "subject": "Fresh", "date": "2026-05-30"}])
    r2 = dt.run_digest(db, "me@x.com", _spec(), now=d2)
    assert r2["status"] == "sent" and r2["count"] == 1          # only the NEW message

    # day 3: nothing new (only the already-digested m1/m2/m3) → empty, no send
    d3 = datetime(2026, 5, 31, 9, 0, tzinfo=ZoneInfo("UTC"))
    _stub_fetch(monkeypatch, _ITEMS + [{"id": "m3", "from": "c@z.com", "subject": "Fresh", "date": "2026-05-30"}])
    r3 = dt.run_digest(db, "me@x.com", _spec(), now=d3)
    assert r3["status"] == "empty"
    assert len(calls) == 2                                      # day1 + day2 only


def test_run_due_digests_weekly_respects_weekday(db, monkeypatch):
    monkeypatch.setattr(dt, "_digest_config", lambda: _cfg())
    monkeypatch.setattr(dt, "load_digests",
                        lambda: [_spec(schedule="weekly", weekday=4, hour=9, minute=0)])  # Friday 9am
    _stub_fetch(monkeypatch, _ITEMS)
    calls = _stub_send(monkeypatch)
    monkeypatch.setattr(dt, "build_digest_body", lambda items, **k: "B")
    tz = ZoneInfo("UTC")
    wed = dt.run_due_digests(db, "me@x.com", now=datetime(2026, 5, 27, 12, 0, tzinfo=tz))  # Wednesday
    assert wed == [] and calls == []                      # not its day yet
    fri = dt.run_due_digests(db, "me@x.com", now=datetime(2026, 5, 29, 9, 0, tzinfo=tz))   # Friday 9am
    assert fri and fri[0]["status"] == "sent" and len(calls) == 1
