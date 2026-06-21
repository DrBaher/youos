"""Approve → create a Google Calendar event — the calendar send frontier.

This is the one place that turns an approved meeting confirmation into a real
Google Calendar event (with a Google Meet link and, when there are attendees,
invite emails). Because invites email the attendees it crosses the never-send
boundary, so it is gated exactly like ``app/agent/send.py``:

* ``agent.outbound_kill_switch`` — when true, blocks everything;
* ``agent.send.enabled`` — the master programmatic-send switch (default false);
* ``agent.calendar.create_events.enabled`` — the dedicated opt-in (default
  false, network-locked);
* ``agent.calendar.daily_event_cap`` — a per-day blast-radius cap.

All four must be open. Detection (``meeting_confirm``) only queues a row; this
is the only path that writes to the calendar, and only on an explicit approval.
``shadow`` / ``dry_run`` exercise the full path without creating anything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.agent import event_store

logger = logging.getLogger(__name__)


@dataclass
class EventOutcome:
    ok: bool
    created: bool = False
    event_id: str | None = None
    meet_link: str | None = None
    html_link: str | None = None
    shadow: bool = False
    created_already: bool = False
    row: dict | None = None
    http_status: int | None = None
    detail: str | None = None


def _event_config() -> dict[str, Any]:
    """Read the calendar-event gates. Defaults are all the safe value (creation
    disabled, kill-switch off-but-irrelevant-when-disabled)."""
    from app.agent.send import _send_config
    from app.core.config import load_config

    cfg = load_config() or {}
    a = (cfg.get("agent") or {}) if isinstance(cfg, dict) else {}
    cal = (a.get("calendar") or {}) if isinstance(a, dict) else {}
    create = (cal.get("create_events") or {}) if isinstance(cal, dict) else {}
    enabled = bool(create.get("enabled", False)) if isinstance(create, dict) else False
    try:
        cap = int(cal.get("daily_event_cap", 5)) if isinstance(cal, dict) else 5
    except (TypeError, ValueError):
        cap = 5
    send = _send_config()
    return {
        "enabled": enabled,
        "daily_event_cap": max(0, cap),
        "send_enabled": bool(send["enabled"]),
        "kill_switch": bool(send["kill_switch"]),
    }


def _gate_block_reason(cfg: dict[str, Any], database_url: str, account: str) -> str | None:
    """The first shut gate, or None when every gate is open. Order matters: the
    kill-switch and send frontier are checked before the dedicated opt-in so the
    message names the outermost blocker first."""
    if cfg["kill_switch"]:
        return "outbound kill-switch is on; all sending is blocked"
    if not cfg["send_enabled"]:
        return "sending is disabled (set agent.send.enabled to allow it)"
    if not cfg["enabled"]:
        return "calendar event creation is disabled (set agent.calendar.create_events.enabled)"
    if cfg["daily_event_cap"] <= 0:
        return "calendar event creation is disabled (agent.calendar.daily_event_cap is 0)"
    if event_store.count_events_created_today(database_url, account=account) >= cfg["daily_event_cap"]:
        return f"daily calendar-event cap reached ({cfg['daily_event_cap']})"
    return None


def apply_pending_event(
    database_url: str,
    row_id: int,
    *,
    shadow: bool = False,
    dry_run: bool = False,
    backend: str | None = None,
) -> EventOutcome:
    """Create the Google Calendar event for an approved pending-event row,
    idempotently and gated.

    Gating (checked before any calendar call): kill-switch → send frontier →
    the dedicated opt-in → the daily cap. A blocked attempt records 'blocked'
    on the row and never claims it. ``shadow`` records the approval without
    touching the calendar; ``dry_run`` runs the real CLI with its no-change flag.
    """
    row = event_store.get_pending_event(database_url, row_id)
    if not row:
        return EventOutcome(False, http_status=404, detail="pending event not found")
    if row["status"] == "created":
        return EventOutcome(
            True, created_already=True, event_id=row.get("event_id"),
            meet_link=row.get("meet_link"), html_link=row.get("html_link"), row=row,
        )
    if row["status"] == "dismissed":
        return EventOutcome(False, http_status=409, detail="event was dismissed")

    cfg = _event_config()
    block = _gate_block_reason(cfg, database_url, row["account"])
    if block is not None:
        # A shut gate must not consume the row — leave it 'pending' so the user
        # can open the gate and approve again.
        event_store.note_event_block(database_url, row_id, detail=block)
        return EventOutcome(False, http_status=403, detail=block,
                            row=event_store.get_pending_event(database_url, row_id))

    state = event_store.claim_event_create(database_url, row_id)
    if state == "missing":
        return EventOutcome(False, http_status=404, detail="pending event not found")
    if state == "dismissed":
        return EventOutcome(False, http_status=409, detail="event was dismissed before the create claimed it")
    if state == "already_done":
        return EventOutcome(True, created_already=True, row=event_store.get_pending_event(database_url, row_id))
    if state == "race_lost":
        return EventOutcome(False, http_status=409, detail="a create for this event is already in progress")

    # state == "claimed": we own the create.
    # Re-assert the daily cap AFTER claiming — the pre-claim check (above) has a
    # TOCTOU window where two concurrent approvals of DIFFERENT rows could each
    # see count==cap-1 and both proceed. The per-row claim serializes the same
    # row; this tightens the cross-row race to near-atomic. Roll the claim back.
    if cfg["daily_event_cap"] > 0 and \
            event_store.count_events_created_today(database_url, account=row["account"]) >= cfg["daily_event_cap"]:
        event_store.abort_event_create(database_url, row_id)
        return EventOutcome(False, http_status=403,
                            detail=f"daily calendar-event cap reached ({cfg['daily_event_cap']})",
                            row=event_store.get_pending_event(database_url, row_id))

    if shadow:
        event_store.abort_event_create(database_url, row_id)
        logger.info("SHADOW calendar event for row %s — not actually created", row_id)
        return EventOutcome(True, shadow=True, row=event_store.get_pending_event(database_url, row_id),
                            detail="shadow mode: not created")

    attendees = [a for a in (row.get("attendees") or []) if a]
    # Full invites when there's someone to invite; a self-only block otherwise.
    send_updates = "all" if attendees else "none"

    from app.ingestion import gmail_write

    try:
        result = gmail_write.create_calendar_event(
            account=row["account"],
            title=row["title"],
            start_iso=row["start_iso"],
            end_iso=row["end_iso"],
            timezone=row.get("timezone"),
            attendees=attendees,
            description=row.get("description"),
            with_meet=True,
            send_updates=send_updates,
            dry_run=dry_run,
            backend=backend,
        )
    except NotImplementedError as exc:
        event_store.abort_event_create(database_url, row_id)
        return EventOutcome(False, http_status=501, detail=str(exc))
    except gmail_write.GmailWriteError as exc:
        event_store.mark_event_error(database_url, row_id, detail=str(exc))
        return EventOutcome(False, http_status=502, detail=f"calendar create failed: {exc}",
                            row=event_store.get_pending_event(database_url, row_id))
    except Exception as exc:  # noqa: BLE001 — never leave a row stuck in 'creating'
        event_store.mark_event_error(database_url, row_id, detail=f"unexpected: {exc}")
        logger.warning("apply_pending_event: unexpected error for row %s: %s", row_id, exc)
        return EventOutcome(False, http_status=500, detail=f"create failed: {exc}",
                            row=event_store.get_pending_event(database_url, row_id))

    if dry_run:
        # gog ran with --dry-run; nothing was created. Roll the claim back.
        event_store.abort_event_create(database_url, row_id)
        return EventOutcome(True, shadow=True, row=event_store.get_pending_event(database_url, row_id),
                            detail="dry-run (gog --dry-run): not created")

    event_store.finalize_event(
        database_url, row_id,
        event_id=result.event_id, meet_link=result.meet_link, html_link=result.html_link,
    )
    return EventOutcome(
        True, created=True, event_id=result.event_id, meet_link=result.meet_link,
        html_link=result.html_link, row=event_store.get_pending_event(database_url, row_id),
    )
