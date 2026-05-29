"""Calendar free/busy → proposed slots."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.agent import calendar


def _first_weekday_on_or_after(d):
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def test_open_slots_no_busy_one_per_weekday():
    now = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)  # 8am
    slots = calendar.compute_open_slots([], now=now, tz="UTC", business_days=3, max_slots=3)
    assert len(slots) == 3
    assert all(s.weekday() < 5 for s, _ in slots)          # weekdays only
    assert all(9 <= s.hour < 17 for s, _ in slots)         # within work hours
    assert len({s.date() for s, _ in slots}) == 3          # one per day (spread)
    assert all((e - s) == timedelta(minutes=30) for s, e in slots)


def test_busy_pushes_slot_later_in_the_day():
    now = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)
    day1 = _first_weekday_on_or_after(now.date())
    # Block 09:00–10:00 UTC on day1 → first slot should start at 10:00.
    busy = [(
        datetime.combine(day1, datetime.min.time(), tzinfo=timezone.utc).replace(hour=9),
        datetime.combine(day1, datetime.min.time(), tzinfo=timezone.utc).replace(hour=10),
    )]
    slots = calendar.compute_open_slots(busy, now=now, tz="UTC", business_days=1, max_slots=1)
    assert len(slots) == 1
    assert slots[0][0].hour == 10 and slots[0][0].minute == 0


def test_no_past_times_today():
    # Now is 3pm; today's remaining slots must start at/after 3pm (rounded up).
    now = datetime(2026, 6, 1, 15, 0, tzinfo=timezone.utc)
    slots = calendar.compute_open_slots([], now=now, tz="UTC", business_days=1, max_slots=1)
    if slots:  # day1 is a weekday
        assert slots[0][0] >= now


def test_format_slots_renders_times():
    s = datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc)
    txt = calendar.format_slots([(s, s + timedelta(minutes=30))])
    assert "2:00" in txt and "Jun 2" in txt


def test_fetch_busy_parses_gog_shape(monkeypatch):
    payload = {
        "calendars": {
            "you@gmail.com": {"busy": [
                {"start": "2026-05-29T14:00:00Z", "end": "2026-05-29T17:00:00Z"},
                {"start": "2026-05-30T08:00:00Z", "end": "2026-05-30T10:00:00Z"},
            ]},
            "holiday@group.calendar.google.com": {"errors": [{"reason": "notFound"}]},
        }
    }
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gog")
    monkeypatch.setattr(
        "app.agent.calendar.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
    )
    busy = calendar.fetch_busy("you@gmail.com", from_iso="x", to_iso="y")
    assert len(busy) == 2  # error calendar skipped
    assert busy[0][0].hour == 14


def test_fetch_busy_nongog_raises(monkeypatch):
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gws")
    import pytest
    with pytest.raises(NotImplementedError):
        calendar.fetch_busy("x@y.com", from_iso="a", to_iso="b")
