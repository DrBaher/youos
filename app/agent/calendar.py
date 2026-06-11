"""Calendar free/busy → proposed meeting slots.

When the agent drafts a reply to a meeting request, it can read the user's
free/busy and offer concrete open times ("Tue 2–2:30pm or Wed 10–10:30am?")
instead of the useless "happy to meet, when works for you?".

Reads free/busy directly via the configured Google CLI (``gog calendar
freebusy`` — verified shape v0.17.0: ``--account <e> --all --from <rfc3339>
--to <rfc3339> --json`` → ``{"calendars": {<id>: {"busy": [{start,end}]}}}``).
gws/native aren't wired yet (raise NotImplementedError). The agent stays
draft-only — it never creates events; it just proposes times the human sends.

``compute_open_slots`` is a pure function (timezone-aware, deterministic given
``now``) so it's unit-tested without a calendar.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

CAL_TIMEOUT_SECONDS = 20


class CalendarFetchError(RuntimeError):
    """free/busy could not be fetched (b246) — distinct from a successfully
    fetched, genuinely free calendar (which is an empty busy list)."""


def _parse_rfc3339(s: str) -> datetime:
    dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_WEEKDAY_NUMS = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4, "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}


def parse_preferred_weekdays(value) -> set[int] | None:
    """Parse a config ``preferred_weekdays`` list (b266) — names ('tue'/'thu',
    case-insensitive) or 0–6 ints — into a weekday-number set, or None when
    unset/empty/all-invalid (= no restriction, every weekday allowed)."""
    if not value:
        return None
    if isinstance(value, (str, int)):
        value = [value]
    out: set[int] = set()
    try:
        for item in value:
            s = str(item).strip().lower()
            if s in _WEEKDAY_NUMS:
                out.add(_WEEKDAY_NUMS[s])
            elif s.isdigit() and 0 <= int(s) <= 6:
                out.add(int(s))
    except TypeError:
        return None
    return out or None


def fetch_busy(
    account: str,
    *,
    from_iso: str,
    to_iso: str,
    backend: str | None = None,
) -> list[tuple[datetime, datetime]]:
    """Return busy intervals (UTC datetimes) for ``account`` between
    ``from_iso`` and ``to_iso`` (RFC3339), unioned across the account's
    calendars. Calendars that error (e.g. an unshared holiday cal) are skipped.
    Only the ``gog`` backend is wired; others raise NotImplementedError."""
    from app.core.config import get_ingestion_google_backend

    name = (backend or get_ingestion_google_backend()).strip().lower()
    if name != "gog":
        raise NotImplementedError(
            f"calendar free/busy is only wired for the gog backend (got {name!r})"
        )

    cmd = [
        "gog", "calendar", "freebusy", "--account", account, "--all",
        "--from", from_iso, "--to", to_iso, "--json", "--no-input",
    ]
    # Fail CLOSED on any backend failure (b246): returning [] made an unknown
    # account / missing gog / timeout / garbage output indistinguishable from
    # a genuinely FREE calendar — drafts then confidently offered meeting
    # times with zero calendar knowledge (audit probe: exit-4 "account not
    # found" produced the same slot proposals as a real empty calendar).
    # propose_open_slots catches this and omits the slot offer instead.
    from app.ingestion.adapters import require_account_argv

    require_account_argv(cmd)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=CAL_TIMEOUT_SECONDS)
    except FileNotFoundError as exc:
        raise CalendarFetchError("gog not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise CalendarFetchError(f"gog freebusy timed out ({CAL_TIMEOUT_SECONDS}s)") from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:200]
        raise CalendarFetchError(f"gog freebusy exit {result.returncode}: {stderr or 'no stderr'}")

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise CalendarFetchError("gog freebusy returned non-JSON output") from exc

    busy: list[tuple[datetime, datetime]] = []
    cals = payload.get("calendars", {}) if isinstance(payload, dict) else {}
    for cal in cals.values():
        if not isinstance(cal, dict):
            continue
        for block in cal.get("busy", []) or []:
            try:
                busy.append((_parse_rfc3339(block["start"]), _parse_rfc3339(block["end"])))
            except (KeyError, ValueError):
                continue
    return busy


def compute_open_slots(
    busy: list[tuple[datetime, datetime]],
    *,
    now: datetime,
    tz: str = "UTC",
    business_days: int = 5,
    work_start_hour: int = 9,
    work_end_hour: int = 17,
    slot_minutes: int = 30,
    max_slots: int = 3,
    preferred_weekdays: set[int] | None = None,
) -> list[tuple[datetime, datetime]]:
    """Compute up to ``max_slots`` open meeting slots (one per business day, for
    spread) over the next ``business_days`` weekdays, within work hours in the
    user's timezone, not overlapping ``busy``. Pure + deterministic given
    ``now``. Returns tz-aware (user-tz) ``(start, end)`` tuples.

    ``preferred_weekdays`` (b266), when given, restricts proposals to those
    weekday numbers (Mon=0 … Sun=6) — e.g. ``{1, 3}`` for Tue/Thu only. The
    horizon stretches to find ``business_days`` *preferred* days."""
    # A non-positive slot length makes the slot-scan loop never advance (step=0)
    # or run backwards — an infinite loop that hangs the triage worker. No valid
    # slots exist for it, so bail. (slot_minutes can come from config, which on a
    # no-PIN instance is settable over the network.)
    if slot_minutes <= 0:
        return []
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = timezone.utc

    busy_local = sorted(
        (s.astimezone(zone), e.astimezone(zone)) for s, e in busy
    )
    now_local = now.astimezone(zone)
    step = timedelta(minutes=slot_minutes)

    out: list[tuple[datetime, datetime]] = []
    days_seen = 0
    offset = 0
    # Scan calendar days until we've covered `business_days` weekdays or filled
    # max_slots. A wider offset cap so a restrictive preferred_weekdays set
    # (e.g. Tue/Thu only) can still find enough days; days_seen only counts the
    # days we actually consider, so the cap never over-runs in the common case.
    max_offset = business_days * 3 + 14
    while days_seen < business_days and len(out) < max_slots and offset <= max_offset:
        day = (now_local + timedelta(days=offset)).date()
        offset += 1
        if day.weekday() >= 5:  # Sat/Sun
            continue
        if preferred_weekdays is not None and day.weekday() not in preferred_weekdays:
            continue  # b266: not a preferred meeting day
        days_seen += 1

        win_start = datetime.combine(day, time(work_start_hour), tzinfo=zone)
        win_end = datetime.combine(day, time(work_end_hour), tzinfo=zone)
        if win_start < now_local:
            # No past times; round up to the next slot boundary.
            minutes = (now_local.minute // slot_minutes + 1) * slot_minutes
            win_start = now_local.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=minutes)

        cursor = win_start
        while cursor + step <= win_end:
            slot_end = cursor + step
            overlaps = any(s < slot_end and cursor < e for s, e in busy_local)
            if not overlaps:
                out.append((cursor, slot_end))
                break  # one slot per day for spread
            cursor = slot_end
    return out[:max_slots]


def format_slots(slots: list[tuple[datetime, datetime]]) -> str:
    """Human-friendly slot list, e.g. 'Tue May 30, 2:00–2:30 PM; Wed May 31,
    10:00–10:30 AM'. Times are shown in whatever tz the slots carry."""
    def _fmt_time(dt: datetime) -> str:
        return dt.strftime("%-I:%M %p") if hasattr(dt, "strftime") else str(dt)

    parts = []
    for s, e in slots:
        parts.append(f"{s.strftime('%a %b %-d')}, {_fmt_time(s)}–{_fmt_time(e)}")
    return "; ".join(parts)


def propose_open_slot_intervals(
    account: str,
    *,
    now: datetime | None = None,
    tz: str = "UTC",
    business_days: int = 5,
    work_start_hour: int = 9,
    work_end_hour: int = 17,
    slot_minutes: int = 30,
    max_slots: int = 3,
    backend: str | None = None,
    extra_busy: list[tuple[datetime, datetime]] | None = None,
    preferred_weekdays: set[int] | None = None,
) -> list[tuple[datetime, datetime]]:
    """Fetch free/busy and return the open slots as ``(start, end)`` intervals
    (empty on any failure). ``extra_busy`` is unioned into the calendar's busy
    set before computing openings — used to avoid offering a slot another
    queued draft already proposed (b265). Failure-isolated."""
    now = now or datetime.now(timezone.utc)
    try:
        horizon = now + timedelta(days=business_days + 9)
        busy = fetch_busy(
            account,
            from_iso=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            to_iso=horizon.strftime("%Y-%m-%dT%H:%M:%SZ"),
            backend=backend,
        )
    except NotImplementedError:
        return []
    except Exception as exc:
        # WARN (b246): a failed fetch now means the draft silently loses its
        # slot proposal — that should be visible, not a debug-level mystery.
        logger.warning("calendar: free/busy fetch failed, omitting slot proposal: %s", exc)
        return []
    if extra_busy:
        busy = list(busy) + list(extra_busy)
    return compute_open_slots(
        busy, now=now, tz=tz, business_days=business_days,
        work_start_hour=work_start_hour, work_end_hour=work_end_hour,
        slot_minutes=slot_minutes, max_slots=max_slots,
        preferred_weekdays=preferred_weekdays,
    )


def propose_open_slots(
    account: str,
    *,
    now: datetime | None = None,
    tz: str = "UTC",
    business_days: int = 5,
    work_start_hour: int = 9,
    work_end_hour: int = 17,
    slot_minutes: int = 30,
    max_slots: int = 3,
    backend: str | None = None,
    extra_busy: list[tuple[datetime, datetime]] | None = None,
    preferred_weekdays: set[int] | None = None,
) -> str:
    """Fetch free/busy and return a prompt-ready 'open slots' string (empty on
    any failure). Failure-isolated: a calendar problem never breaks drafting."""
    return format_slots(
        propose_open_slot_intervals(
            account, now=now, tz=tz, business_days=business_days,
            work_start_hour=work_start_hour, work_end_hour=work_end_hour,
            slot_minutes=slot_minutes, max_slots=max_slots, backend=backend,
            extra_busy=extra_busy, preferred_weekdays=preferred_weekdays,
        )
    )
