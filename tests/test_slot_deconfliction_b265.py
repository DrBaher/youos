"""b265: queued meeting drafts must not all offer the same time.

Each draft picks the first open slot per day, so without coordination every
meeting draft proposes identical times — send them all and you double-book.
Proposed slots are stored structured and fed as 'busy' to the next proposal.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.agent import calendar, store


def _dt(h, m=0, day=12):
    return datetime(2026, 6, day, h, m, tzinfo=timezone.utc)


def test_extra_busy_excludes_already_proposed(monkeypatch):
    """propose_open_slot_intervals unions extra_busy into the calendar busy
    set, so a slot another draft already took isn't offered again."""
    # Calendar itself is wide open.
    monkeypatch.setattr(calendar, "fetch_busy", lambda *a, **k: [])
    now = _dt(8)  # 8am, before the work day
    first = calendar.compute_open_slots([], now=now, business_days=1, max_slots=1)
    assert first  # there's an opening
    # Feed that opening back as extra_busy → next proposal must differ.
    second = calendar.propose_open_slot_intervals(
        "a@x.com", now=now, business_days=1, max_slots=1, extra_busy=first
    )
    assert second and second[0] != first[0]  # distinct slot


@pytest.fixture
def db(tmp_path, monkeypatch):
    from pathlib import Path
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


def _seed(db_url, mid):
    return store.upsert_pending(
        db_url, message_id=mid, thread_id="t-" + mid, account="you@example.com",
        sender="A <a@x.com>", sender_email="a@x.com", subject="Meet?", body="meet?",
        received_at="2026-06-01T10:00:00Z", needs_reply_score=0.8, reasons=[],
        cold_outreach=False, tier="draft", draft="d", draft_model="q",
        draft_repairs=[], standing_instructions_snapshot="You are free at: x",
    )


def test_proposed_slots_roundtrip_and_pending_only(db):
    rid1 = _seed(db, "m1")
    rid2 = _seed(db, "m2")
    slots = [(_dt(9, 30), _dt(10, 0)), (_dt(11, 0), _dt(11, 30))]
    store.set_proposed_slots(db, rid1, slots)

    got = store.pending_proposed_slots(db, "you@example.com")
    assert len(got) == 2
    assert got[0][0] == _dt(9, 30)

    # exclude_row_id drops that row's slots (so a draft doesn't block itself).
    assert store.pending_proposed_slots(db, "you@example.com", exclude_row_id=rid1) == []

    # A dismissed draft frees its slots — they no longer count as taken.
    store.set_proposed_slots(db, rid2, [(_dt(14, 0), _dt(14, 30))])
    import sqlite3
    c = sqlite3.connect(db.removeprefix("sqlite:///"))
    c.execute("UPDATE agent_pending_drafts SET status='dismissed' WHERE id=?", (rid2,))
    c.commit()
    c.close()
    taken = store.pending_proposed_slots(db, "you@example.com")
    assert all(s[0] != _dt(14, 0) for s in taken)  # dismissed row's slot freed


def test_two_sweep_drafts_get_distinct_slots(db, monkeypatch):
    """End-to-end through the helper + accumulator pattern: feeding one draft's
    slots as exclusion makes the next draft propose different times."""
    monkeypatch.setattr(calendar, "fetch_busy", lambda *a, **k: [])
    monkeypatch.setattr(
        "app.agent.triage._calendar_config",
        lambda: {"enabled": True, "tz": "UTC", "business_days": 3,
                 "work_start_hour": 9, "work_end_hour": 17, "slot_minutes": 30, "max_slots": 3},
    )
    from app.agent import triage

    acc: list = []
    note1, s1 = triage._calendar_slot_note("you@example.com", exclude_busy=acc)
    acc.extend(s1)
    note2, s2 = triage._calendar_slot_note("you@example.com", exclude_busy=acc)
    assert s1 and s2
    # No slot is shared between the two drafts.
    assert set(s1).isdisjoint(set(s2))
