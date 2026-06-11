"""b266: restrict meeting-slot proposals to preferred weekdays.

The user can set agent.calendar.preferred_weekdays (e.g. ["tue","thu"]) so the
agent only offers times on those days; the daily range stays
work_start_hour/work_end_hour.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.agent.calendar import compute_open_slots, parse_preferred_weekdays


def test_parse_weekdays_names_ints_and_none():
    assert parse_preferred_weekdays(["Tue", "thu"]) == {1, 3}
    assert parse_preferred_weekdays(["monday", "FRIDAY"]) == {0, 4}
    assert parse_preferred_weekdays([1, 3]) == {1, 3}
    assert parse_preferred_weekdays("wed") == {2}
    assert parse_preferred_weekdays(None) is None
    assert parse_preferred_weekdays([]) is None
    assert parse_preferred_weekdays(["nonsense"]) is None  # all-invalid → no restriction


def test_slots_only_on_preferred_weekdays():
    # Mon Jun 1 2026 08:00 UTC. Weekdays: Mon=1, Tue=2, Wed=3, Thu=4, Fri=5.
    now = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)
    slots = compute_open_slots(
        [], now=now, business_days=5, max_slots=5, slot_minutes=30,
        preferred_weekdays={1, 3},  # Tue, Thu only
    )
    assert slots
    for s, _e in slots:
        assert s.weekday() in (1, 3)  # never Mon/Wed/Fri


def test_unrestricted_still_uses_all_weekdays():
    now = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)
    slots = compute_open_slots([], now=now, business_days=3, max_slots=3, preferred_weekdays=None)
    weekdays = {s.weekday() for s, _ in slots}
    assert weekdays == {0, 1, 2}  # Mon, Tue, Wed — consecutive, no restriction


def test_preferred_horizon_reaches_far_enough():
    """Tue/Thu-only with business_days=3 must still find 3 slots within the
    widened horizon, not stop short."""
    now = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)  # Monday
    slots = compute_open_slots(
        [], now=now, business_days=3, max_slots=3, preferred_weekdays={1, 3},
    )
    assert len(slots) == 3  # Tue Jun 2, Thu Jun 4, Tue Jun 9
    assert [s.weekday() for s, _ in slots] == [1, 3, 1]


def test_calendar_config_parses_preferred(monkeypatch):
    from app.agent import triage

    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {
        "agent": {"calendar": {"enabled": True, "preferred_weekdays": ["tue", "thu"]}},
        "user": {"timezone": "UTC"},
    })
    cfg = triage._calendar_config()
    assert cfg["preferred_weekdays"] == {1, 3}
