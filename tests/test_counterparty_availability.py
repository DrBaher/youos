"""Direction C (b287): detect a meeting time/range the COUNTERPARTY states in
an inbound, intersect a window with the user's free/busy, queue one slot.

Covers the extractor (`detect_counterparty_availability` / `_parse_availability`),
the availability intersection (`_resolve_counterparty_slot`), and the triage
wrapper that queues a pending event.
"""

from __future__ import annotations

import sqlite3
import types
from datetime import datetime, timezone

from app.agent import event_store
from app.agent.meeting_confirm import (
    AvailabilityResult,
    _parse_availability,
    detect_counterparty_availability,
)
from app.db.bootstrap import _migrate_agent_pending_events

VIENNA = "Europe/Vienna"


# --- extractor -------------------------------------------------------------


def test_specific_time_extracted():
    r = detect_counterparty_availability(
        subject="Re: Sync", sender="G", sender_email="g@newmetrics.com",
        body="Thursday at 3pm works for me.", tz=VIENNA,
        complete_fn=lambda p: "FINAL: AT 2026-07-09T15:00:00+02:00",
    )
    assert r.kind == "specific"
    assert r.start_iso.startswith("2026-07-09T15:00")
    assert r.end_iso.startswith("2026-07-09T15:30")  # + slot_minutes default
    assert r.attendees == ["g@newmetrics.com"]


def test_range_extracted():
    r = detect_counterparty_availability(
        subject="Re: Sync", sender="G", sender_email="g@newmetrics.com",
        body="I'm free any afternoon next week, let's schedule a call.", tz=VIENNA,
        complete_fn=lambda p: "FINAL: RANGE 2026-07-06T12:00:00+02:00..2026-07-10T17:00:00+02:00",
    )
    assert r.kind == "range"
    assert r.start_iso.startswith("2026-07-06T12:00")
    assert r.end_iso.startswith("2026-07-10T17:00")


def test_vague_returns_none():
    r = detect_counterparty_availability(
        subject="hi", sender="G", sender_email="g@x.com",
        body="Let's meet sometime, we'll sort timing later.", tz=VIENNA,
        complete_fn=lambda p: "FINAL: NONE",
    )
    assert r is None


def test_no_scheduling_signal_skips_model():
    called = []

    def _fn(p):
        called.append(p)
        return "FINAL: AT 2026-07-09T15:00:00+02:00"

    r = detect_counterparty_availability(
        subject="hi", sender="G", sender_email="g@x.com",
        body="Thanks for the docs, looks good.", tz=VIENNA, complete_fn=_fn,
    )
    assert r is None
    assert called == []  # cheap pre-filter kept the model out of it


def test_self_sender_excluded():
    r = detect_counterparty_availability(
        subject="hi", sender="me", sender_email="baher@medicus.ai",
        body="meet thursday 3pm", account_emails={"baher@medicus.ai"}, tz=VIENNA,
        complete_fn=lambda p: "FINAL: AT 2026-07-09T15:00:00+02:00",
    )
    assert r is None  # no one to invite


# --- FINAL-line parsing edge cases -----------------------------------------


def test_parse_backwards_range_rejected():
    assert _parse_availability(
        "FINAL: RANGE 2026-07-10T17:00:00+02:00..2026-07-06T12:00:00+02:00", VIENNA, 30
    ) is None


def test_parse_none_and_missing():
    assert _parse_availability("FINAL: NONE", VIENNA, 30) is None
    assert _parse_availability("I don't know", VIENNA, 30) is None


def test_parse_naive_localized_to_tz():
    kind, s, _e = _parse_availability("FINAL: AT 2026-07-09T15:00:00", VIENNA, 30)
    assert kind == "specific"
    assert "+02:00" in s  # naive datetime anchored to Europe/Vienna (CEST)


# --- availability intersection (_resolve_counterparty_slot) ----------------


def _cfg(**over):
    base = {"tz": "UTC", "slot_minutes": 30, "work_start_hour": 9,
            "work_end_hour": 17, "max_slots": 3, "preferred_weekdays": None}
    base.update(over)
    return base


def _res(kind, start, end):
    return AvailabilityResult(kind=kind, start_iso=start, end_iso=end,
                              title="Sync", attendees=["g@x.com"], confidence=0.7, reasons=[])


def test_specific_honored_and_flags_conflict(monkeypatch):
    import app.agent.calendar as cal
    from app.agent.triage import _resolve_counterparty_slot
    # busy over exactly the confirmed slot → conflict note, but still proposed.
    monkeypatch.setattr(cal, "fetch_busy", lambda *a, **k: [
        (datetime(2027, 1, 6, 14, 0, tzinfo=timezone.utc),
         datetime(2027, 1, 6, 15, 0, tzinfo=timezone.utc))])
    s, e, reasons = _resolve_counterparty_slot(
        "acct", _res("specific", "2027-01-06T14:00:00+00:00", "2027-01-06T14:30:00+00:00"), _cfg())
    assert s == "2027-01-06T14:00:00+00:00"  # confirmed time honored as-is
    assert "conflicts" in reasons[0]


def test_specific_no_conflict(monkeypatch):
    import app.agent.calendar as cal
    from app.agent.triage import _resolve_counterparty_slot
    monkeypatch.setattr(cal, "fetch_busy", lambda *a, **k: [])
    s, _e, reasons = _resolve_counterparty_slot(
        "acct", _res("specific", "2027-01-06T14:00:00+00:00", "2027-01-06T14:30:00+00:00"), _cfg())
    assert s == "2027-01-06T14:00:00+00:00"
    assert "conflicts" not in reasons[0]


def test_range_picks_first_free_slot(monkeypatch):
    import app.agent.calendar as cal
    from app.agent.triage import _resolve_counterparty_slot
    # Monday fully busy → the picked slot must fall on Tue+ within the window.
    monkeypatch.setattr(cal, "fetch_busy", lambda *a, **k: [
        (datetime(2027, 1, 4, 0, 0, tzinfo=timezone.utc),
         datetime(2027, 1, 4, 23, 59, tzinfo=timezone.utc))])
    s, e, reasons = _resolve_counterparty_slot(
        "acct", _res("range", "2027-01-04T00:00:00+00:00", "2027-01-08T23:59:00+00:00"), _cfg())
    picked = datetime.fromisoformat(s)
    assert picked.weekday() >= 1  # not Monday (Monday was busy)
    assert datetime.fromisoformat("2027-01-04T00:00:00+00:00") <= picked
    assert datetime.fromisoformat(e) <= datetime.fromisoformat("2027-01-08T23:59:00+00:00")
    assert reasons and "proposing" in reasons[0]


def test_range_fail_closed_when_freebusy_unavailable(monkeypatch):
    import app.agent.calendar as cal
    from app.agent.triage import _resolve_counterparty_slot

    def _boom(*a, **k):
        raise cal.CalendarFetchError("gog down")

    monkeypatch.setattr(cal, "fetch_busy", _boom)
    s, e, reasons = _resolve_counterparty_slot(
        "acct", _res("range", "2027-01-04T00:00:00+00:00", "2027-01-08T23:59:00+00:00"), _cfg())
    assert (s, e, reasons) == (None, None, None)  # never propose with zero calendar knowledge


# --- triage wrapper queues a pending event ---------------------------------


def test_wrapper_queues_event(tmp_path, monkeypatch):
    import app.agent.calendar as cal
    import app.agent.meeting_confirm as mc
    from app.agent import triage

    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    _migrate_agent_pending_events(conn)
    conn.close()
    url = f"sqlite:///{path}"

    monkeypatch.setattr(cal, "fetch_busy", lambda *a, **k: [])
    monkeypatch.setattr(mc, "detect_counterparty_availability", lambda **kw: _res(
        "specific", "2027-01-06T14:00:00+00:00", "2027-01-06T14:30:00+00:00"))

    msg = types.SimpleNamespace(
        thread_id="T1", message_id="M1", subject="Re: Sync",
        sender="G", sender_email="g@x.com", body="thursday 3pm works", received_at=None,
        headers={},
    )
    cfg = _cfg(tz="Europe/Vienna", preferred_weekdays={2, 3, 4})
    queued = triage._maybe_detect_counterparty_availability(
        url, "acct", msg, account_emails=["baher@medicus.ai"], cal_cfg=cfg)
    assert queued is True
    row = event_store.get_event_by_thread(url, "T1")
    assert row and row["start_iso"] == "2027-01-06T14:00:00+00:00"
    assert row["status"] == "pending"

    # idempotent: a second run must not double-queue.
    assert triage._maybe_detect_counterparty_availability(
        url, "acct", msg, account_emails=["baher@medicus.ai"], cal_cfg=cfg) is False


# --- deterministic on-device range fallback (no model, private) ------------

from app.agent.meeting_confirm import _deterministic_range  # noqa: E402

_NOW = "2026-07-03T10:00:00+02:00"  # a Friday


def test_deterministic_next_week():
    kind, s, e = _deterministic_range("let's do next week", _NOW, VIENNA)
    assert kind == "range"
    assert s.startswith("2026-07-06")  # Monday
    assert e.startswith("2026-07-10")  # Friday


def test_deterministic_weekday():
    kind, s, _e = _deterministic_range("I'm free Wednesday", _NOW, VIENNA)
    assert kind == "range"
    assert s.startswith("2026-07-08")  # the next Wednesday


def test_deterministic_none_on_vague():
    assert _deterministic_range("let's catch up sometime soon", _NOW, VIENNA) is None


def test_range_fallback_fires_when_model_misses(monkeypatch):
    # Model returns NONE (as the local 4B does on ranges) → deterministic
    # fallback recovers "next week" on-device.
    r = detect_counterparty_availability(
        subject="Re: Sync", sender="G", sender_email="g@x.com",
        body="Yes, next week is good on my end.", now_iso=_NOW, tz=VIENNA,
        complete_fn=lambda p: "FINAL: NONE",
    )
    assert r is not None and r.kind == "range"
    assert r.start_iso.startswith("2026-07-06")


def test_availability_phrase_passes_prefilter():
    # "free Wednesday" has no scheduling verb but must still be considered.
    called = []
    detect_counterparty_availability(
        subject="hi", sender="G", sender_email="g@x.com",
        body="I'm free Wednesday afternoon.", now_iso=_NOW, tz=VIENNA,
        complete_fn=lambda p: called.append(p) or "FINAL: NONE",
    )
    assert called, "availability phrase should reach the model (broadened pre-filter)"


# --- b287 false-positive guard (live bug: newsletters queued as invites) ----


def test_newsletter_prose_not_a_meeting():
    # Incidental "this week"/"Sunday"/"works" with no scheduling intent → NONE.
    for body in (
        "The Monocle Weekend Edition. This week: top stories, read on Sunday.",
        "3-2-1: How luck works this week, why improvement is hard, a call for courage.",
        "Invoice INV-2026-0806 from Medicus AI is due next week. Please remit.",
    ):
        r = detect_counterparty_availability(
            subject="x", sender="S", sender_email="s@ext.com", body=body,
            now_iso=_NOW, tz=VIENNA, complete_fn=lambda p: "FINAL: NONE",
        )
        assert r is None, f"should not detect a meeting in: {body!r}"


def test_scheduling_intent_required_for_range():
    # A weekday word alone must NOT trigger the deterministic range fallback.
    assert detect_counterparty_availability(
        subject="x", sender="S", sender_email="s@ext.com",
        body="See you at the Wednesday conference keynote.",
        now_iso=_NOW, tz=VIENNA, complete_fn=lambda p: "FINAL: NONE",
    ) is None
