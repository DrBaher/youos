"""Persistence for the calendar-event approval queue (``agent_pending_events``).

Mirrors the draft queue in ``app/agent/store.py`` but for the
auto-confirm → approve → create-event flow:

* the detector (``meeting_confirm``) calls :func:`queue_pending_event` when it
  spots an accepted slot — idempotent on (account, thread, start);
* the user approves a row → :func:`claim_event_create` is the cross-process
  serialization point (``pending`` → ``creating``), exactly like
  ``store.begin_send`` for a draft;
* :func:`finalize_event` records the created event (id + Meet link);
* :func:`mark_event_error` / :func:`dismiss_event` close a row out.

Creating the event is gated + performed in ``app/agent/calendar_events.py``;
this module only owns the row state.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Any

from app.agent.store import _connect


def queue_pending_event(
    database_url: str,
    *,
    account: str,
    thread_id: str,
    title: str,
    start_iso: str,
    end_iso: str,
    message_id: str | None = None,
    source_draft_id: int | None = None,
    timezone: str | None = None,
    attendees: list[str] | None = None,
    description: str | None = None,
    confidence: float | None = None,
    reasons: list[str] | None = None,
) -> int | None:
    """Queue a calendar event for the user's approval. Returns the new row id, or
    ``None`` when a live event for the same (account, thread, start) is already
    queued/creating/created — the partial UNIQUE index makes this the detector's
    idempotency point (re-running triage won't duplicate a proposal)."""
    with closing(_connect(database_url)) as conn:
        try:
            cur = conn.execute(
                """INSERT INTO agent_pending_events
                   (account, thread_id, message_id, source_draft_id, title,
                    start_iso, end_iso, timezone, attendees_json, description,
                    confidence, reasons_json, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    account, thread_id, message_id, source_draft_id, title,
                    start_iso, end_iso, timezone,
                    json.dumps(list(attendees or [])), description,
                    confidence, json.dumps(list(reasons or [])),
                ),
            )
            conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["attendees"] = json.loads(d.get("attendees_json") or "[]")
    d["reasons"] = json.loads(d.get("reasons_json") or "[]")
    return d


def list_pending_events(
    database_url: str, *, status: str = "pending", limit: int = 50
) -> list[dict[str, Any]]:
    """Rows in a given status, newest first (default the awaiting-approval queue)."""
    with closing(_connect(database_url)) as conn:
        rows = conn.execute(
            "SELECT * FROM agent_pending_events WHERE status = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (status, max(1, int(limit))),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_pending_event(database_url: str, row_id: int) -> dict[str, Any] | None:
    with closing(_connect(database_url)) as conn:
        row = conn.execute(
            "SELECT * FROM agent_pending_events WHERE id = ?", (row_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def claim_event_create(database_url: str, row_id: int) -> str:
    """Atomically claim a pending event for creation. The conditional UPDATE
    ``pending`` → ``creating`` is the cross-process serialization point, so two
    approvals can't double-book. Returns:

      * ``"missing"``     — no such row
      * ``"dismissed"``   — already dismissed
      * ``"already_done"``— already created
      * ``"race_lost"``   — another create is in flight (status='creating')
      * ``"claimed"``     — caller won; follow with finalize/abort/error
    """
    with closing(_connect(database_url)) as conn:
        row = conn.execute(
            "SELECT status FROM agent_pending_events WHERE id = ?", (row_id,)
        ).fetchone()
        if row is None:
            return "missing"
        if row["status"] == "dismissed":
            return "dismissed"
        if row["status"] == "created":
            return "already_done"
        cur = conn.execute(
            "UPDATE agent_pending_events SET status = 'creating', "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'pending'",
            (row_id,),
        )
        conn.commit()
        if cur.rowcount == 0:
            r2 = conn.execute(
                "SELECT status FROM agent_pending_events WHERE id = ?", (row_id,)
            ).fetchone()
            if r2 and r2["status"] == "dismissed":
                return "dismissed"
            if r2 and r2["status"] == "created":
                return "already_done"
            return "race_lost"
        return "claimed"


def finalize_event(
    database_url: str,
    row_id: int,
    *,
    event_id: str,
    meet_link: str = "",
    html_link: str = "",
) -> None:
    """Record a successfully created event."""
    with closing(_connect(database_url)) as conn:
        conn.execute(
            "UPDATE agent_pending_events SET status = 'created', event_id = ?, "
            "meet_link = ?, html_link = ?, created_event_at = CURRENT_TIMESTAMP, "
            "detail = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (event_id, meet_link, html_link, row_id),
        )
        conn.commit()


def abort_event_create(database_url: str, row_id: int) -> None:
    """Roll a claimed-but-not-created row back to ``pending`` so it can be
    approved again — used for a dry-run (gog ran but created nothing) and for a
    pre-create transient failure."""
    with closing(_connect(database_url)) as conn:
        conn.execute(
            "UPDATE agent_pending_events SET status = 'pending', "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'creating'",
            (row_id,),
        )
        conn.commit()


def note_event_block(database_url: str, row_id: int, *, detail: str) -> None:
    """Record why an approval was blocked by a shut gate, WITHOUT consuming the
    row — it stays 'pending' so the user can open the gate (e.g. flip
    agent.send.enabled) and approve again. Only touches a pending row."""
    with closing(_connect(database_url)) as conn:
        conn.execute(
            "UPDATE agent_pending_events SET detail = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'pending'",
            (f"blocked: {detail}"[:500], row_id),
        )
        conn.commit()


def mark_event_error(database_url: str, row_id: int, *, detail: str) -> None:
    """Terminal failure after the create was claimed. Kept conservative (not
    auto-retried) so a partial/ambiguous failure can't double-book — the user
    can dismiss it and a fresh detection may re-queue (the slot UNIQUE index
    excludes 'error' rows)."""
    with closing(_connect(database_url)) as conn:
        conn.execute(
            "UPDATE agent_pending_events SET status = 'error', detail = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (detail[:500], row_id),
        )
        conn.commit()


def dismiss_event(database_url: str, row_id: int, *, note: str | None = None) -> bool:
    """Dismiss a queued event (the user declined to create it). Returns False if
    the row was already created (you can't un-create) or doesn't exist."""
    with closing(_connect(database_url)) as conn:
        cur = conn.execute(
            "UPDATE agent_pending_events SET status = 'dismissed', "
            "dismissal_note = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status != 'created'",
            (note, row_id),
        )
        conn.commit()
        return cur.rowcount > 0


def count_events_created_today(database_url: str, *, account: str | None = None) -> int:
    """Events actually created today (UTC) — for the daily-cap gate."""
    sql = (
        "SELECT COUNT(*) FROM agent_pending_events WHERE status = 'created' "
        "AND created_event_at IS NOT NULL AND date(created_event_at) = date('now')"
    )
    params: tuple = ()
    if account:
        sql += " AND account = ?"
        params = (account,)
    with closing(_connect(database_url)) as conn:
        return int(conn.execute(sql, params).fetchone()[0])
