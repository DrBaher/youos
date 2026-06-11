"""b246: calendar free/busy fails CLOSED — a fetch failure must never be
indistinguishable from a genuinely free calendar.

Audit probe: a gog exit-4 ("account not found") shim produced byte-identical
slot proposals to a real empty calendar; drafts then offered meeting times
with zero calendar knowledge.
"""

from __future__ import annotations

import subprocess

import pytest

from app.agent import calendar
from app.agent.calendar import CalendarFetchError, fetch_busy, propose_open_slots


class _R:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def test_fetch_busy_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        "app.agent.calendar.subprocess.run",
        lambda *a, **k: _R(rc=4, stderr="account not found"),
    )
    with pytest.raises(CalendarFetchError, match="exit 4"):
        fetch_busy("a@x.com", from_iso="x", to_iso="y")


def test_fetch_busy_raises_on_garbage_output(monkeypatch):
    monkeypatch.setattr("app.agent.calendar.subprocess.run", lambda *a, **k: _R(stdout="not json"))
    with pytest.raises(CalendarFetchError, match="non-JSON"):
        fetch_busy("a@x.com", from_iso="x", to_iso="y")


def test_fetch_busy_raises_on_missing_gog_and_timeout(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("gog")

    monkeypatch.setattr("app.agent.calendar.subprocess.run", missing)
    with pytest.raises(CalendarFetchError, match="not on PATH"):
        fetch_busy("a@x.com", from_iso="x", to_iso="y")

    def hangs(*a, **k):
        raise subprocess.TimeoutExpired(cmd="gog", timeout=20)

    monkeypatch.setattr("app.agent.calendar.subprocess.run", hangs)
    with pytest.raises(CalendarFetchError, match="timed out"):
        fetch_busy("a@x.com", from_iso="x", to_iso="y")


def test_fetch_busy_refuses_empty_account():
    with pytest.raises(ValueError, match="empty --account"):
        fetch_busy("", from_iso="x", to_iso="y")


def test_propose_open_slots_empty_on_fetch_failure(monkeypatch):
    def failing_fetch(*a, **k):
        raise CalendarFetchError("gog freebusy exit 4: account not found")

    monkeypatch.setattr(calendar, "fetch_busy", failing_fetch)
    assert propose_open_slots("a@x.com") == ""  # no fabricated availability


def test_propose_open_slots_works_for_genuinely_free_calendar(monkeypatch):
    monkeypatch.setattr(calendar, "fetch_busy", lambda *a, **k: [])
    slots = propose_open_slots("a@x.com")
    assert slots  # an actually-empty calendar still proposes times
