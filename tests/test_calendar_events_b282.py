"""b282: auto-detect a confirmed meeting → queue → approve → create event.

Three layers under test, none touching the network:

* the detector (``meeting_confirm``) with an injected ``complete_fn``;
* the gated create path (``calendar_events.apply_pending_event``) with the gog
  backend's ``subprocess.run`` mocked — the gating matrix is the safety core;
* the ``gog calendar create`` argv shape, asserted against the verified flags
  (``--with-meet``, ``--send-updates``, ``--attendees``) so a CLI drift is caught
  here rather than in production (the "mocked tests can't catch wrong CLI" trap —
  the real shape was confirmed via ``gog calendar create --help`` + a --dry-run).

The never-send invariant: with default config nothing is created, and the
create_events flag is network-locked so a token can't self-arm it.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.agent import calendar_events, event_store
from app.agent.meeting_confirm import _parse_choice, _strip_re, detect_confirmation
from app.db.bootstrap import _migrate_agent_pending_events
from app.ingestion import gmail_write

# All gates open — the OPEN config the create tests patch in.
OPEN = {"enabled": True, "daily_event_cap": 5, "send_enabled": True, "kill_switch": False}


@pytest.fixture
def db(tmp_path):
    """A DB with the pending-events table and one queued event. Returns
    (database_url, row_id)."""
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    _migrate_agent_pending_events(conn)
    conn.close()
    url = f"sqlite:///{path}"
    rid = event_store.queue_pending_event(
        url, account="me@x.com", thread_id="t1", message_id="m1",
        title="Project sync", start_iso="2030-01-01T15:00:00-05:00",
        end_iso="2030-01-01T15:30:00-05:00", attendees=["al@partner.com"],
        confidence=0.85, reasons=["confirmed slot #1"],
    )
    assert rid is not None
    return url, rid


def _ok_create(**_kw):
    return gmail_write.CalendarEventResult(
        event_id="evt_1", meet_link="https://meet.google.com/abc-defg-hij",
        html_link="https://calendar.google.com/evt_1", raw_response={},
    )


def _must_not_create(**_kw):
    raise AssertionError("a shut gate must never call create_calendar_event")


# --- detector ---------------------------------------------------------------


def test_detector_accepts_one_slot():
    slots = [["2030-01-01T14:00:00-05:00", "2030-01-01T14:30:00-05:00"],
             ["2030-01-02T10:00:00-05:00", "2030-01-02T10:30:00-05:00"]]
    r = detect_confirmation(
        subject="Re: Project sync", sender="Al", sender_email="al@b.com",
        body="Tuesday 2pm works great, see you then!", proposed_slots=slots,
        account_emails={"me@x.com"}, complete_fn=lambda p: "1",
    )
    assert r is not None
    assert r.start_iso == "2030-01-01T14:00:00-05:00"
    assert r.title == "Project sync"
    assert r.attendees == ["al@b.com"]


def test_detector_declines_and_vague_return_none():
    slots = [["2030-01-01T14:00:00-05:00", "2030-01-01T14:30:00-05:00"]]
    assert detect_confirmation(
        subject="x", sender="Al", sender_email="al@b.com",
        body="none of those work", proposed_slots=slots, complete_fn=lambda p: "NONE",
    ) is None


def test_detector_no_proposed_slots_skips_model():
    called = {"n": 0}

    def fn(p):
        called["n"] += 1
        return "1"

    assert detect_confirmation(
        subject="x", sender="a", sender_email="a@b.com", body="ok",
        proposed_slots=[], complete_fn=fn,
    ) is None
    assert called["n"] == 0  # never bothered the model


def test_detector_excludes_self_from_attendees():
    slots = [["2030-01-01T14:00:00-05:00", "2030-01-01T14:30:00-05:00"]]
    r = detect_confirmation(
        subject="sync", sender="me", sender_email="me@x.com", body="works",
        proposed_slots=slots, account_emails={"me@x.com"}, complete_fn=lambda p: "1",
    )
    assert r is not None and r.attendees == []


def test_parse_choice_bounds_and_words():
    # terse path (on-device model)
    assert _parse_choice("1", 2) == 0
    assert _parse_choice("slot 2", 2) == 1
    assert _parse_choice("#2", 2) == 1
    assert _parse_choice("option 3", 3) == 2
    assert _parse_choice("5", 2) is None        # out of range
    assert _parse_choice("NONE", 2) is None
    assert _parse_choice("", 2) is None
    # Prose that merely CONTAINS a digit must not be mined into a confirmation —
    # the leading-anchor guards against a model that explains instead of obeying.
    assert _parse_choice("the person did not confirm, so 1 would be wrong", 2) is None
    assert _parse_choice("None — they asked for slot 2 to move", 2) is None
    # reasoning path: a model that thinks out loud then commits on FINAL: — we
    # read its conclusion, not its first cautious token.
    assert _parse_choice("NONE — wait, a thumbs-up is acceptance.\nFINAL: 1", 2) == 0
    assert _parse_choice("They asked to move it.\nFINAL: NONE", 2) is None
    assert _parse_choice("FINAL: 2", 3) == 1
    assert _parse_choice("FINAL: 9", 3) is None  # out of range even via FINAL


def test_strip_re_prefixes():
    assert _strip_re("Re: Re: Fwd: Hello") == "Hello"
    assert _strip_re("") == "Meeting"


def test_parse_choice_rejects_conditional_terse_answers():
    # A hedged answer leading with a number must NOT confirm (no FINAL line).
    assert _parse_choice("2, but only if my flight lands on time", 3) is None
    assert _parse_choice("1 tentatively", 2) is None
    assert _parse_choice("3?", 3) is None
    assert _parse_choice("1 unless something comes up", 2) is None
    # A clean terse number still confirms.
    assert _parse_choice("2", 3) == 1
    # A FINAL line still wins even with hedging prose before it.
    assert _parse_choice("hmm, maybe... FINAL: 1", 2) == 0


def test_reap_stale_creating(db):
    url, rid = db
    # claim it → 'creating', then backdate updated_at so the reaper frees it.
    assert event_store.claim_event_create(url, rid) == "claimed"
    import sqlite3

    from app.db.bootstrap import resolve_sqlite_path
    conn = sqlite3.connect(resolve_sqlite_path(url))
    conn.execute("UPDATE agent_pending_events SET updated_at = datetime('now','-30 minutes') WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    assert event_store.reap_stale_creating(url) == 1
    assert event_store.get_pending_event(url, rid)["status"] == "pending"
    # a freshly-claimed row is NOT reaped.
    event_store.claim_event_create(url, rid)
    assert event_store.reap_stale_creating(url) == 0


def test_detector_model_routing_uses_selected_tier(monkeypatch):
    from app.agent import meeting_confirm

    seen = {}

    def fake_select(tier, **kw):
        seen["tier"] = tier
        return lambda p: "1"

    monkeypatch.setattr("app.core.completion.select_completion", fake_select)
    slots = [["2030-01-01T14:00:00-05:00", "2030-01-01T14:30:00-05:00"]]
    r = meeting_confirm.detect_confirmation(
        subject="x", sender="A", sender_email="a@b.com", body="works",
        proposed_slots=slots, model="cloud",
    )
    assert r is not None and seen["tier"] == "cloud"


def test_detector_cloud_unavailable_falls_back_to_local(monkeypatch):
    from app.agent import meeting_confirm

    calls = []

    def fake_select(tier, **kw):
        calls.append(tier)
        return None if tier == "cloud" else (lambda p: "1")

    monkeypatch.setattr("app.core.completion.select_completion", fake_select)
    slots = [["2030-01-01T14:00:00-05:00", "2030-01-01T14:30:00-05:00"]]
    r = meeting_confirm.detect_confirmation(
        subject="x", sender="A", sender_email="a@b.com", body="works",
        proposed_slots=slots, model="cloud",
    )
    assert r is not None              # fell back, still detected
    assert calls == ["cloud", "local"]


def test_detector_returns_none_when_no_model_available(monkeypatch):
    from app.agent import meeting_confirm

    monkeypatch.setattr("app.core.completion.select_completion", lambda *a, **k: None)
    slots = [["2030-01-01T14:00:00-05:00", "2030-01-01T14:30:00-05:00"]]
    assert meeting_confirm.detect_confirmation(
        subject="x", sender="A", sender_email="a@b.com", body="works",
        proposed_slots=slots, model="local",
    ) is None


# --- gating matrix (the safety core) ----------------------------------------


@pytest.mark.parametrize("cfg,needle", [
    ({"enabled": True, "daily_event_cap": 5, "send_enabled": True, "kill_switch": True}, "kill-switch"),
    ({"enabled": True, "daily_event_cap": 5, "send_enabled": False, "kill_switch": False}, "agent.send.enabled"),
    ({"enabled": False, "daily_event_cap": 5, "send_enabled": True, "kill_switch": False}, "create_events"),
    ({"enabled": True, "daily_event_cap": 0, "send_enabled": True, "kill_switch": False}, "daily_event_cap"),
])
def test_shut_gate_blocks_and_leaves_pending(db, monkeypatch, cfg, needle):
    url, rid = db
    monkeypatch.setattr(calendar_events, "_event_config", lambda: cfg)
    monkeypatch.setattr(gmail_write, "create_calendar_event", _must_not_create)
    out = calendar_events.apply_pending_event(url, rid)
    assert not out.ok and out.http_status == 403
    assert needle in (out.detail or "")
    # A shut gate must NOT consume the row — still approvable later.
    assert event_store.get_pending_event(url, rid)["status"] == "pending"


def test_default_config_never_creates(tmp_path, monkeypatch):
    """With a real default config (nothing patched), approval is refused —
    proves the gate isn't an artifact of the OPEN stub."""
    import app.core.config as config_mod

    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "youos_config.yaml").write_text("agent:\n  enabled: true\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_dir / "youos_config.yaml")
    config_mod.load_config.cache_clear()

    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    _migrate_agent_pending_events(conn)
    conn.close()
    url = f"sqlite:///{path}"
    rid = event_store.queue_pending_event(
        url, account="me@x.com", thread_id="t1", title="x",
        start_iso="2030-01-01T15:00:00Z", end_iso="2030-01-01T15:30:00Z",
    )
    monkeypatch.setattr(gmail_write, "create_calendar_event", _must_not_create)
    try:
        out = calendar_events.apply_pending_event(url, rid)
        assert not out.ok and out.http_status == 403
    finally:
        config_mod.load_config.cache_clear()


def test_daily_cap_rechecked_after_claim(db, monkeypatch):
    """TOCTOU: the cap is re-asserted AFTER the claim. Simulate the race — the
    pre-claim count is under the cap, but by post-claim another create landed —
    and assert no event is created."""
    url, rid = db
    monkeypatch.setattr(calendar_events, "_event_config",
                        lambda: {"enabled": True, "daily_event_cap": 1, "send_enabled": True, "kill_switch": False})
    counts = iter([0, 1])  # gate-check sees 0; post-claim re-check sees 1
    monkeypatch.setattr(event_store, "count_events_created_today", lambda *a, **k: next(counts))
    monkeypatch.setattr(gmail_write, "create_calendar_event", _must_not_create)
    out = calendar_events.apply_pending_event(url, rid)
    assert not out.ok and "cap reached" in (out.detail or "")
    # claim was rolled back → row is back to pending (re-approvable)
    assert event_store.get_pending_event(url, rid)["status"] == "pending"


def test_daily_cap_reached_blocks(db, monkeypatch):
    url, rid = db
    monkeypatch.setattr(calendar_events, "_event_config", lambda: OPEN)
    # Pretend the cap is already met.
    monkeypatch.setattr(event_store, "count_events_created_today", lambda *a, **k: 5)
    monkeypatch.setattr(gmail_write, "create_calendar_event", _must_not_create)
    out = calendar_events.apply_pending_event(url, rid)
    assert not out.ok and "cap reached" in (out.detail or "")


# --- create path ------------------------------------------------------------


def test_create_when_all_gates_open(db, monkeypatch):
    url, rid = db
    monkeypatch.setattr(calendar_events, "_event_config", lambda: OPEN)
    monkeypatch.setattr(gmail_write, "create_calendar_event", _ok_create)
    out = calendar_events.apply_pending_event(url, rid)
    assert out.ok and out.created
    assert out.meet_link == "https://meet.google.com/abc-defg-hij"
    row = event_store.get_pending_event(url, rid)
    assert row["status"] == "created" and row["event_id"] == "evt_1"
    assert event_store.count_events_created_today(url) == 1


def test_create_passes_attendees_and_send_updates_all(db, monkeypatch):
    url, rid = db
    captured = {}

    def _capture(**kw):
        captured.update(kw)
        return _ok_create()

    monkeypatch.setattr(calendar_events, "_event_config", lambda: OPEN)
    monkeypatch.setattr(gmail_write, "create_calendar_event", _capture)
    calendar_events.apply_pending_event(url, rid)
    assert captured["attendees"] == ["al@partner.com"]
    assert captured["send_updates"] == "all"   # invites go out
    assert captured["with_meet"] is True


def test_no_attendees_uses_send_updates_none(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    _migrate_agent_pending_events(conn)
    conn.close()
    url = f"sqlite:///{path}"
    rid = event_store.queue_pending_event(
        url, account="me@x.com", thread_id="t9", title="Solo block",
        start_iso="2030-02-01T15:00:00Z", end_iso="2030-02-01T15:30:00Z",
    )
    captured = {}
    monkeypatch.setattr(calendar_events, "_event_config", lambda: OPEN)
    monkeypatch.setattr(gmail_write, "create_calendar_event",
                        lambda **kw: captured.update(kw) or _ok_create())
    calendar_events.apply_pending_event(url, rid)
    assert captured["send_updates"] == "none"   # self-only, emails nobody


def test_dry_run_reverts_to_pending(db, monkeypatch):
    url, rid = db
    captured = {}
    monkeypatch.setattr(calendar_events, "_event_config", lambda: OPEN)
    monkeypatch.setattr(gmail_write, "create_calendar_event",
                        lambda **kw: captured.update(kw) or _ok_create())
    out = calendar_events.apply_pending_event(url, rid, dry_run=True)
    assert out.ok and out.shadow
    assert captured["dry_run"] is True
    assert event_store.get_pending_event(url, rid)["status"] == "pending"
    assert event_store.count_events_created_today(url) == 0


def test_shadow_reverts_to_pending_without_calling_gog(db, monkeypatch):
    url, rid = db
    monkeypatch.setattr(calendar_events, "_event_config", lambda: OPEN)
    monkeypatch.setattr(gmail_write, "create_calendar_event", _must_not_create)
    out = calendar_events.apply_pending_event(url, rid, shadow=True)
    assert out.ok and out.shadow
    assert event_store.get_pending_event(url, rid)["status"] == "pending"


def test_idempotent_reapprove(db, monkeypatch):
    url, rid = db
    monkeypatch.setattr(calendar_events, "_event_config", lambda: OPEN)
    monkeypatch.setattr(gmail_write, "create_calendar_event", _ok_create)
    calendar_events.apply_pending_event(url, rid)
    # Second approval doesn't create again.
    monkeypatch.setattr(gmail_write, "create_calendar_event", _must_not_create)
    out = calendar_events.apply_pending_event(url, rid)
    assert out.ok and out.created_already


def test_backend_error_is_terminal_error(db, monkeypatch):
    url, rid = db
    monkeypatch.setattr(calendar_events, "_event_config", lambda: OPEN)
    monkeypatch.setattr(
        gmail_write, "create_calendar_event",
        lambda **kw: (_ for _ in ()).throw(gmail_write.GmailWriteError("gog exit 4")),
    )
    out = calendar_events.apply_pending_event(url, rid)
    assert not out.ok and out.http_status == 502
    assert event_store.get_pending_event(url, rid)["status"] == "error"


# --- store: claim + idempotency ---------------------------------------------


def test_queue_idempotent_on_slot(db):
    url, _ = db
    # Same (account, thread, start) as the fixture row → no duplicate.
    dup = event_store.queue_pending_event(
        url, account="me@x.com", thread_id="t1", title="dup",
        start_iso="2030-01-01T15:00:00-05:00", end_iso="2030-01-01T15:30:00-05:00",
    )
    assert dup is None
    assert len(event_store.list_pending_events(url)) == 1


def test_claim_is_at_most_once(db):
    url, rid = db
    assert event_store.claim_event_create(url, rid) == "claimed"
    # A second claim while the first is in flight loses the race.
    assert event_store.claim_event_create(url, rid) == "race_lost"


def test_dismiss_event(db):
    url, rid = db
    assert event_store.dismiss_event(url, rid, note="not needed") is True
    assert event_store.get_pending_event(url, rid)["status"] == "dismissed"


# --- gog argv shape (verified against the real CLI) -------------------------


def test_gog_create_event_builds_verified_command(monkeypatch):
    captured = {}

    class _Result:
        returncode = 0
        # REAL gog 0.22.0 shape: the Event resource is wrapped under "event"
        # (verified against live output — a flat {"id":...} would be wrong).
        stdout = ('{"event": {"id": "evt_9", "hangoutLink": "https://meet.google.com/x-y-z",'
                  ' "htmlLink": "https://cal/evt_9"}}')
        stderr = ""

    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run",
                        lambda cmd, **kw: captured.update(cmd=cmd) or _Result())
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gog")
    res = gmail_write.create_calendar_event(
        account="me@x.com", title="Sync", start_iso="2030-01-01T15:00:00Z",
        end_iso="2030-01-01T15:30:00Z", timezone="America/New_York",
        attendees=["a@b.com", "c@d.com"], with_meet=True, send_updates="all",
    )
    assert res.event_id == "evt_9"
    assert res.meet_link == "https://meet.google.com/x-y-z"
    cmd = captured["cmd"]
    assert cmd[:4] == ["gog", "calendar", "create", "primary"]
    # Attacker-influenced strings use the =-form (option-injection guard).
    assert "--summary=Sync" in cmd
    assert "--with-meet" in cmd
    assert "--attendees=a@b.com,c@d.com" in cmd
    assert cmd[cmd.index("--send-updates") + 1] == "all"
    assert "--start-timezone=America/New_York" in cmd
    assert "--json" in cmd and "--no-input" in cmd
    assert "--dry-run" not in cmd


def test_gog_create_event_leading_dash_subject_not_a_flag(monkeypatch):
    """A '--'-leading subject (attacker-controlled) must ride as a =-joined value,
    never a parsed flag."""
    captured = {}

    class _R:
        returncode = 0
        stdout = '{"event": {"id": "e"}}'
        stderr = ""

    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", lambda cmd, **kw: captured.update(cmd=cmd) or _R())
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gog")
    gmail_write.create_calendar_event(
        account="me@x.com", title="--send-updates=all sneaky", start_iso="2030-01-01T15:00:00Z",
        end_iso="2030-01-01T15:30:00Z", send_updates="none",
    )
    assert "--summary=--send-updates=all sneaky" in captured["cmd"]
    # the only standalone --send-updates token is the real one, value 'none'
    assert captured["cmd"][captured["cmd"].index("--send-updates") + 1] == "none"


def test_create_event_rejects_bad_send_updates(monkeypatch):
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gog")
    with pytest.raises(gmail_write.GmailWriteError):
        gmail_write.create_calendar_event(
            account="me@x.com", title="x", start_iso="2030-01-01T15:00:00Z",
            end_iso="2030-01-01T15:30:00Z", send_updates="everyone",
        )


def test_gog_create_event_dry_run_passes_flag(monkeypatch):
    captured = {}

    class _Result:
        returncode = 0
        stdout = '{"dry_run": true}'
        stderr = ""

    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run",
                        lambda cmd, **kw: captured.update(cmd=cmd) or _Result())
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gog")
    gmail_write.create_calendar_event(
        account="me@x.com", title="Sync", start_iso="2030-01-01T15:00:00Z",
        end_iso="2030-01-01T15:30:00Z", dry_run=True,
    )
    assert "--dry-run" in captured["cmd"]


def test_gog_create_event_unwraps_event_envelope_and_meet_from_conf(monkeypatch):
    """gog wraps the Event under "event" and the Meet URL may only be in
    conferenceData.entryPoints — the create path must read both (the live shape
    that the flat-mock tests originally missed)."""
    class _Result:
        returncode = 0
        stdout = ('{"event": {"id": "evt_x", "htmlLink": "https://cal/evt_x",'
                  ' "conferenceData": {"entryPoints": ['
                  '{"entryPointType": "more", "uri": "https://meet.google.com/s"},'
                  '{"entryPointType": "video", "uri": "https://meet.google.com/real"}]}}}')
        stderr = ""

    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", lambda cmd, **kw: _Result())
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gog")
    res = gmail_write.create_calendar_event(
        account="me@x.com", title="S", start_iso="2030-01-01T15:00:00Z", end_iso="2030-01-01T15:30:00Z",
    )
    assert res.event_id == "evt_x"
    assert res.meet_link == "https://meet.google.com/real"
    assert res.html_link == "https://cal/evt_x"


def test_gog_create_event_dry_run_tolerates_request_envelope(monkeypatch):
    """A --dry-run returns {"dry_run": true, "request": {...}} with no event —
    that must not raise (no id is expected in dry-run)."""
    class _Result:
        returncode = 0
        stdout = '{"dry_run": true, "op": "calendar.create", "request": {}}'
        stderr = ""

    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", lambda cmd, **kw: _Result())
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gog")
    res = gmail_write.create_calendar_event(
        account="me@x.com", title="S", start_iso="2030-01-01T15:00:00Z",
        end_iso="2030-01-01T15:30:00Z", dry_run=True,
    )
    assert res.event_id == "" and res.meet_link == ""


def test_meet_link_falls_back_to_entry_points():
    payload = {"id": "e", "conferenceData": {"entryPoints": [
        {"entryPointType": "more", "uri": "https://meet.google.com/settings"},
        {"entryPointType": "video", "uri": "https://meet.google.com/real-link"},
    ]}}
    assert gmail_write._meet_link_from_event(payload) == "https://meet.google.com/real-link"


# --- send-frontier lock -----------------------------------------------------


def test_create_events_flag_is_network_locked():
    from app.core.feature_flags import SEND_FRONTIER_FLAGS, SendFrontierWriteError, set_flag

    assert "agent.calendar.create_events.enabled" in SEND_FRONTIER_FLAGS
    with pytest.raises(SendFrontierWriteError):
        set_flag("agent.calendar.create_events.enabled", True, allow_send_frontier=False)


# --- REST endpoints (TestClient) --------------------------------------------


@pytest.fixture
def api(monkeypatch, tmp_path):
    """App + DB with one queued event. Returns (client, database_url, row_id)."""
    from pathlib import Path

    from fastapi.testclient import TestClient

    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("YOUOS_DATABASE_URL", f"sqlite:///{tmp_path}/var/youos.db")
    (tmp_path / "var").mkdir(exist_ok=True)
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    repo_schema = Path(__file__).resolve().parents[1] / "docs" / "schema.sql"
    (docs / "schema.sql").write_text(repo_schema.read_text())
    monkeypatch.setattr("app.core.config.CONFIG_PATH", tmp_path / "youos_config.yaml")
    from app.core.config import load_config
    load_config.cache_clear()
    from app.core.settings import get_settings
    get_settings.cache_clear()
    from app.db.bootstrap import bootstrap_database
    bootstrap_database()

    db_url = f"sqlite:///{tmp_path}/var/youos.db"
    rid = event_store.queue_pending_event(
        db_url, account="me@x.com", thread_id="t1", title="Sync",
        start_iso="2030-01-01T15:00:00Z", end_iso="2030-01-01T15:30:00Z",
        attendees=["al@partner.com"],
    )
    from app.main import app
    app.state.settings = get_settings()
    yield TestClient(app), db_url, rid
    get_settings.cache_clear()


def test_api_list_pending_events(api):
    client, _, rid = api
    r = client.get("/api/agent/events/pending")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1 and body["events"][0]["id"] == rid
    assert body["events"][0]["attendees"] == ["al@partner.com"]


def test_api_approve_blocked_under_default_config_leaves_pending(api):
    client, db_url, rid = api
    # Default config: every gate shut → 403, row stays pending.
    r = client.post(f"/api/agent/events/{rid}/approve")
    assert r.status_code == 403
    assert event_store.get_pending_event(db_url, rid)["status"] == "pending"


def test_api_dismiss_event(api):
    client, db_url, rid = api
    r = client.post(f"/api/agent/events/{rid}/dismiss", json={"note": "no"})
    assert r.status_code == 200
    assert event_store.get_pending_event(db_url, rid)["status"] == "dismissed"


def test_api_approve_missing_is_404(api):
    client, _, _ = api
    assert client.post("/api/agent/events/99999/approve").status_code == 404


def test_api_event_by_thread(api):
    client, _, rid = api
    r = client.get("/api/agent/events/by_thread/t1")
    assert r.status_code == 200
    assert r.json()["event"]["id"] == rid
    # unknown thread → 404 (the add-on renders no event card)
    assert client.get("/api/agent/events/by_thread/nope").status_code == 404


# --- triage wiring ----------------------------------------------------------


def test_maybe_detect_confirmation_queues_from_thread_slots(tmp_path, monkeypatch):
    """The sweep hook: a reply on a thread we proposed slots for, that confirms
    one, queues exactly one pending event keyed off the stored slots."""
    import json
    from types import SimpleNamespace

    from app.agent import meeting_confirm, store, triage
    from app.db.bootstrap import _migrate_agent_pending_drafts

    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    _migrate_agent_pending_drafts(conn)
    _migrate_agent_pending_events(conn)
    conn.commit()
    conn.close()
    url = f"sqlite:///{path}"

    # A drafted meeting-request row that proposed two slots (b265 storage shape).
    rid = store.upsert_pending(
        url, message_id="orig", thread_id="t1", account="me@x.com",
        sender="Al <al@b.com>", sender_email="al@b.com", subject="Project sync",
        body="can we meet?", received_at="2030-01-01T09:00:00Z",
        needs_reply_score=0.9, reasons=[], cold_outreach=False, tier="draft",
        draft="How about these times?", draft_model="m", draft_repairs=[],
        standing_instructions_snapshot=None,
    )
    slots = [["2030-01-02T14:00:00-05:00", "2030-01-02T14:30:00-05:00"],
             ["2030-01-03T10:00:00-05:00", "2030-01-03T10:30:00-05:00"]]
    store.set_proposed_slots(url, rid, [
        (__import__("datetime").datetime.fromisoformat(s),
         __import__("datetime").datetime.fromisoformat(e)) for s, e in slots
    ])

    # The acceptance reply (model stubbed to pick slot #1).
    monkeypatch.setattr(
        meeting_confirm, "detect_confirmation",
        lambda **kw: meeting_confirm.ConfirmationResult(
            start_iso=slots[0][0], end_iso=slots[0][1], title="Project sync",
            attendees=["al@b.com"], confidence=0.85, reasons=["confirmed #1"],
        ),
    )
    msg = SimpleNamespace(
        thread_id="t1", message_id="reply-1", subject="Re: Project sync",
        sender="Al <al@b.com>", sender_email="al@b.com", body="The first works!",
    )
    assert triage._maybe_detect_confirmation(url, "me@x.com", msg, account_emails=["me@x.com"]) is True
    pend = event_store.list_pending_events(url)
    assert len(pend) == 1
    assert pend[0]["start_iso"] == slots[0][0]
    assert pend[0]["thread_id"] == "t1"
    assert json.loads(pend[0]["reasons_json"]) == ["confirmed #1"]

    # Idempotent: same detection on a second sweep won't double-queue the slot.
    assert triage._maybe_detect_confirmation(url, "me@x.com", msg, account_emails=["me@x.com"]) is False
    assert len(event_store.list_pending_events(url)) == 1


def test_detect_short_circuits_when_event_exists_even_dismissed(tmp_path, monkeypatch):
    """If the thread already has an event in ANY state (incl. dismissed), the
    detector must NOT call the model again (no repeated off-device egress) and
    must NOT re-queue — prevents duplicate events + re-surfacing a declined one."""
    from types import SimpleNamespace

    from app.agent import event_store, meeting_confirm, store, triage
    from app.db.bootstrap import (
        _migrate_agent_pending_drafts,
        _migrate_agent_pending_events,
    )

    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    _migrate_agent_pending_drafts(conn)
    _migrate_agent_pending_events(conn)
    conn.commit()
    conn.close()
    url = f"sqlite:///{path}"
    rid = store.upsert_pending(
        url, message_id="orig", thread_id="t1", account="me@x.com",
        sender="Al", sender_email="al@b.com", subject="sync", body="meet?",
        received_at="2030-01-01T09:00:00Z", needs_reply_score=0.9, reasons=[],
        cold_outreach=False, tier="draft", draft="d", draft_model="m",
        draft_repairs=[], standing_instructions_snapshot=None,
    )
    store.set_proposed_slots(url, rid, [
        (__import__("datetime").datetime.fromisoformat("2030-01-02T14:00:00-05:00"),
         __import__("datetime").datetime.fromisoformat("2030-01-02T14:30:00-05:00"))])
    # Pre-existing DISMISSED event for the thread.
    ev = event_store.queue_pending_event(url, account="me@x.com", thread_id="t1",
        title="x", start_iso="2030-01-02T14:00:00-05:00", end_iso="2030-01-02T14:30:00-05:00")
    event_store.dismiss_event(url, ev)

    monkeypatch.setattr(meeting_confirm, "detect_confirmation",
                        lambda **kw: pytest.fail("model must not run when an event already exists"))
    msg = SimpleNamespace(thread_id="t1", message_id="reply", subject="Re: sync",
                          sender="Al", sender_email="al@b.com", body="works")
    assert triage._maybe_detect_confirmation(url, "me@x.com", msg, account_emails=["me@x.com"]) is False
    # still exactly one (dismissed) event — nothing re-queued
    assert len(event_store.list_pending_events(url, status="dismissed")) == 1


def test_maybe_detect_confirmation_no_slots_is_noop(tmp_path, monkeypatch):
    """A thread with no proposed slots never queues an event (and never even asks
    the model)."""
    from types import SimpleNamespace

    from app.agent import meeting_confirm, store, triage
    from app.db.bootstrap import _migrate_agent_pending_drafts

    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    _migrate_agent_pending_drafts(conn)
    _migrate_agent_pending_events(conn)
    conn.commit()
    conn.close()
    url = f"sqlite:///{path}"
    store.upsert_pending(
        url, message_id="orig", thread_id="t1", account="me@x.com",
        sender="Al", sender_email="al@b.com", subject="hi", body="hello",
        received_at="2030-01-01T09:00:00Z", needs_reply_score=0.9, reasons=[],
        cold_outreach=False, tier="draft", draft="d", draft_model="m",
        draft_repairs=[], standing_instructions_snapshot=None,
    )
    monkeypatch.setattr(meeting_confirm, "detect_confirmation",
                        lambda **kw: pytest.fail("must not run detection without slots"))
    msg = SimpleNamespace(thread_id="t1", message_id="r1", subject="Re: hi",
                          sender="Al", sender_email="al@b.com", body="ok")
    assert triage._maybe_detect_confirmation(url, "me@x.com", msg) is False
    assert event_store.list_pending_events(url) == []


# --- self-scheduled detection (the user's OWN reply confirms a meeting) -------

def test_detect_self_scheduled_specific_time():
    from app.agent.meeting_confirm import detect_self_scheduled
    r = detect_self_scheduled(
        subject="RE: M42 intro",
        body="Let's lock Tuesday June 24 at 3pm Vienna. I'll send the invite.",
        recipients=["lpetalidis@m42.ae"], account_emails={"baher@medicus.ai"},
        now_iso="Mon, 22 Jun 2026", tz="Europe/Vienna",
        complete_fn=lambda p: "FINAL: 2026-06-24T15:00:00+02:00",
    )
    assert r is not None
    assert r.start_iso.startswith("2026-06-24T15:00")
    assert r.end_iso.startswith("2026-06-24T15:30")   # default 30-min
    assert r.attendees == ["lpetalidis@m42.ae"]
    assert r.confidence == 0.7


def test_detect_self_scheduled_vague_returns_none():
    from app.agent.meeting_confirm import detect_self_scheduled
    r = detect_self_scheduled(
        subject="x", body="Monday or Tuesday PM next week works.", recipients=["x@y.com"],
        account_emails={"me@x.com"}, now_iso="2026-06-22", tz="Europe/Vienna",
        complete_fn=lambda p: "FINAL: NONE",
    )
    assert r is None


def test_detect_self_scheduled_requires_attendee():
    """No recipient = nobody to invite → None, and the model is never called."""
    from app.agent.meeting_confirm import detect_self_scheduled
    called = {"n": 0}
    def _fn(p):
        called["n"] += 1
        return "FINAL: 2026-06-24T15:00:00+02:00"
    r = detect_self_scheduled(
        subject="x", body="Tuesday 3pm", recipients=[], account_emails=set(),
        now_iso="2026-06-22", tz="Europe/Vienna", complete_fn=_fn,
    )
    assert r is None and called["n"] == 0


def test_detect_self_scheduled_naive_datetime_localized_to_tz():
    """A model datetime without an offset is anchored to the configured tz."""
    from app.agent.meeting_confirm import detect_self_scheduled
    r = detect_self_scheduled(
        subject="x", body="Tuesday 3pm", recipients=["x@y.com"], account_emails=set(),
        now_iso="2026-06-22", tz="Europe/Vienna",
        complete_fn=lambda p: "FINAL: 2026-06-24T15:00:00",   # naive
    )
    assert r is not None and ("+02:00" in r.start_iso or "+01:00" in r.start_iso)


def test_detect_self_scheduled_excludes_self_from_attendees():
    from app.agent.meeting_confirm import detect_self_scheduled
    r = detect_self_scheduled(
        subject="x", body="Tuesday 3pm", recipients=["me@x.com", "them@y.com"],
        account_emails={"me@x.com"}, now_iso="2026-06-22", tz="UTC",
        complete_fn=lambda p: "FINAL: 2026-06-24T15:00:00+00:00",
    )
    assert r is not None and r.attendees == ["them@y.com"]
