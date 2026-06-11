"""b264: queued meeting-request drafts whose proposed slots have gone stale
(drafted on a prior day → times now in the past) are re-drafted in place with
current availability, keeping the row in the review queue.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.agent import store, triage


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("YOUOS_DATABASE_URL", f"sqlite:///{tmp_path}/var/youos.db")
    (tmp_path / "var").mkdir()
    (tmp_path / "configs").mkdir()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "schema.sql").write_text((Path(__file__).resolve().parents[1] / "docs" / "schema.sql").read_text())
    monkeypatch.setattr("app.core.config.CONFIG_PATH", tmp_path / "youos_config.yaml")
    from app.core.config import load_config
    load_config.cache_clear()
    from app.core.settings import get_settings
    get_settings.cache_clear()
    from app.db.bootstrap import bootstrap_database
    bootstrap_database()
    return f"sqlite:///{tmp_path}/var/youos.db"


def _seed(db_url, *, snapshot, created_days_ago):
    rid = store.upsert_pending(
        db_url, message_id="m", thread_id="t", account="you@example.com",
        sender="Alice <alice@x.com>", sender_email="alice@x.com",
        subject="Meet?", body="Can we meet next week?",
        received_at="2026-06-01T10:00:00Z", needs_reply_score=0.8, reasons=[],
        cold_outreach=False, tier="draft", draft="Sure — I'm free Mon Jun 2 at 2pm.",
        draft_model="qwen", draft_repairs=[], standing_instructions_snapshot=snapshot,
    )
    c = sqlite3.connect(db_url.removeprefix("sqlite:///"))
    c.execute(
        "UPDATE agent_pending_drafts SET created_at = datetime('now', ?), updated_at = datetime('now', ?) WHERE id = ?",
        (f"-{created_days_ago} days", f"-{created_days_ago} days", rid),
    )
    c.commit()
    c.close()
    return rid


def test_stale_meeting_draft_refreshed_in_place(db, monkeypatch):
    rid = _seed(db, snapshot="The sender is asking to meet. You are free at: Mon Jun 2 2pm.", created_days_ago=5)
    monkeypatch.setattr(triage, "_calendar_slot_note", lambda account, cal_cfg=None, exclude_busy=None: ("You are free at: Thu Jun 18, 2:00 PM.", []))

    fake = type("R", (), {"draft": "Happy to meet — I'm free Thu Jun 18 at 2 PM.", "model_used": "qwen"})()
    monkeypatch.setattr("app.generation.service.generate_draft", lambda req, **k: fake)

    r = triage.refresh_stale_meeting_drafts(db, "you@example.com")
    assert r["scanned"] == 1 and r["refreshed"] == 1

    c = sqlite3.connect(db.removeprefix("sqlite:///"))
    row = c.execute("SELECT draft, status FROM agent_pending_drafts WHERE id=?", (rid,)).fetchone()
    assert "Jun 18" in row[0]              # refreshed with current slot
    assert "Jun 2" not in row[0]           # stale time gone
    assert row[1] == "pending"             # still in the queue
    c.close()


def test_non_meeting_draft_untouched(db, monkeypatch):
    _seed(db, snapshot="just a normal instruction", created_days_ago=5)  # no slot marker
    monkeypatch.setattr(triage, "_calendar_slot_note", lambda account, cal_cfg=None, exclude_busy=None: ("You are free at: Thu.", []))
    called = {"gen": 0}
    monkeypatch.setattr("app.generation.service.generate_draft", lambda req, **k: called.__setitem__("gen", 1))
    r = triage.refresh_stale_meeting_drafts(db, "you@example.com")
    assert r["scanned"] == 0 and called["gen"] == 0


def test_today_meeting_draft_not_stale(db, monkeypatch):
    _seed(db, snapshot="You are free at: Tue 2pm.", created_days_ago=0)  # drafted today
    monkeypatch.setattr(triage, "_calendar_slot_note", lambda account, cal_cfg=None, exclude_busy=None: ("You are free at: Thu.", []))
    r = triage.refresh_stale_meeting_drafts(db, "you@example.com")
    assert r["scanned"] == 0  # today's slots aren't stale yet


def test_no_current_slots_leaves_draft(db, monkeypatch):
    rid = _seed(db, snapshot="You are free at: Mon Jun 2.", created_days_ago=5)
    monkeypatch.setattr(triage, "_calendar_slot_note", lambda account, cal_cfg=None, exclude_busy=None: (None, []))  # calendar full / off
    r = triage.refresh_stale_meeting_drafts(db, "you@example.com")
    assert r["scanned"] == 1 and r["refreshed"] == 0 and r["no_slots"] == 1
    c = sqlite3.connect(db.removeprefix("sqlite:///"))
    assert "Jun 2" in c.execute("SELECT draft FROM agent_pending_drafts WHERE id=?", (rid,)).fetchone()[0]
    c.close()


def test_update_draft_inplace_keeps_status_and_skips_terminal(db):
    rid = _seed(db, snapshot="x", created_days_ago=1)
    assert store.update_draft_inplace(db, rid, draft="fresh text") is True
    c = sqlite3.connect(db.removeprefix("sqlite:///"))
    c.execute("UPDATE agent_pending_drafts SET status='dismissed' WHERE id=?", (rid,))
    c.commit()
    c.close()
    assert store.update_draft_inplace(db, rid, draft="should not apply") is False  # terminal row untouched


def test_calendar_slot_note_marker_present(monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(triage, "_calendar_config", lambda: {
        "enabled": True, "tz": "Europe/Vienna", "business_days": 5,
        "work_start_hour": 9, "work_end_hour": 17, "slot_minutes": 30, "max_slots": 3,
    })
    slot = (datetime(2026, 6, 18, 14, 0, tzinfo=timezone.utc), datetime(2026, 6, 18, 14, 30, tzinfo=timezone.utc))
    monkeypatch.setattr("app.agent.calendar.propose_open_slot_intervals", lambda *a, **k: [slot])
    note, slots = triage._calendar_slot_note("you@example.com")
    assert triage._CAL_SLOT_MARKER in note
    assert slots == [slot]


def test_calendar_slot_note_none_when_no_slots(monkeypatch):
    monkeypatch.setattr(triage, "_calendar_config", lambda: {
        "enabled": True, "tz": "UTC", "business_days": 5,
        "work_start_hour": 9, "work_end_hour": 17, "slot_minutes": 30, "max_slots": 3,
    })
    monkeypatch.setattr("app.agent.calendar.propose_open_slot_intervals", lambda *a, **k: [])
    assert triage._calendar_slot_note("you@example.com") == (None, [])
