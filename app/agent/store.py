"""Persistence for the autonomous-agent loop's triage results.

One row per inbound the agent processed, drafts *and* surface-for-review.
The hard-skipped majority (newsletters, automation, CI mail) is not stored
— it's noise. Idempotent on ``message_id`` so repeated triage runs don't
re-draft the same inbound.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

Tier = Literal["draft", "surface"]
Status = Literal["pending", "amended", "sent", "dismissed"]


def _db_path(database_url: str) -> Path:
    return Path(urllib.parse.urlparse(database_url).path)


def _connect(database_url: str) -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(database_url))
    conn.row_factory = sqlite3.Row
    return conn


def upsert_pending(
    database_url: str,
    *,
    message_id: str,
    thread_id: str,
    account: str,
    sender: str | None,
    sender_email: str | None,
    subject: str | None,
    body: str | None,
    received_at: str | None,
    needs_reply_score: float,
    reasons: list[str],
    cold_outreach: bool,
    tier: Tier,
    draft: str | None,
    draft_model: str | None,
    draft_repairs: list[str] | None,
    standing_instructions_snapshot: str | None,
) -> int | None:
    """Insert a triage result if the ``message_id`` isn't already stored.

    Returns the inserted row id, or ``None`` if a row already existed (the
    idempotency case — same unread thread surfacing across multiple triage
    runs shouldn't produce duplicates).
    """
    with closing(_connect(database_url)) as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO agent_pending_drafts (
                message_id, thread_id, account,
                sender, sender_email, subject, body, received_at,
                needs_reply_score, reasons_json, cold_outreach, tier,
                draft, draft_model, draft_repairs_json, standing_instructions_snapshot
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id, thread_id, account,
                sender, sender_email, subject, body, received_at,
                float(needs_reply_score),
                json.dumps(reasons, ensure_ascii=False),
                1 if cold_outreach else 0,
                tier,
                draft, draft_model,
                json.dumps(draft_repairs or [], ensure_ascii=False),
                standing_instructions_snapshot,
            ),
        )
        conn.commit()
        return cur.lastrowid if cur.rowcount > 0 else None


def list_pending(
    database_url: str,
    *,
    account: str | None = None,
    status: Status = "pending",
    tier: Tier | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List pending rows, newest first. ``tier=None`` returns both 'draft' and
    'surface' tiers so the UI can render them together (with the surface
    section collapsed by default)."""
    sql = "SELECT * FROM agent_pending_drafts WHERE status = ?"
    params: list[Any] = [status]
    if account:
        sql += " AND account = ?"
        params.append(account)
    if tier:
        sql += " AND tier = ?"
        params.append(tier)
    sql += " ORDER BY needs_reply_score DESC, created_at DESC LIMIT ?"
    params.append(int(limit))

    with closing(_connect(database_url)) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def get(database_url: str, row_id: int) -> dict[str, Any] | None:
    with closing(_connect(database_url)) as conn:
        row = conn.execute(
            "SELECT * FROM agent_pending_drafts WHERE id = ?", (row_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def mark_amended(database_url: str, row_id: int, *, amended_draft: str) -> bool:
    return _update_status(
        database_url, row_id, status="amended",
        amended_draft=amended_draft,
    )


def mark_sent(
    database_url: str,
    row_id: int,
    *,
    gmail_draft_id: str | None = None,
) -> bool:
    """Mark row as sent. ``gmail_draft_id`` is set by the
    push-to-Gmail path (Phase 2); the plain "Mark sent manually" UX
    leaves it None — same status, no Gmail-side draft reference."""
    return _update_status(
        database_url, row_id, status="sent", sent_at_now=True,
        gmail_draft_id=gmail_draft_id,
    )


def mark_dismissed(database_url: str, row_id: int) -> bool:
    return _update_status(database_url, row_id, status="dismissed", dismissed_at_now=True)


def _update_status(
    database_url: str,
    row_id: int,
    *,
    status: Status,
    amended_draft: str | None = None,
    sent_at_now: bool = False,
    dismissed_at_now: bool = False,
    gmail_draft_id: str | None = None,
) -> bool:
    sets = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
    params: list[Any] = [status]
    if amended_draft is not None:
        sets.append("amended_draft = ?")
        params.append(amended_draft)
    if sent_at_now:
        sets.append("sent_at = CURRENT_TIMESTAMP")
    if dismissed_at_now:
        sets.append("dismissed_at = CURRENT_TIMESTAMP")
    if gmail_draft_id is not None:
        sets.append("gmail_draft_id = ?")
        params.append(gmail_draft_id)
    params.append(row_id)
    sql = f"UPDATE agent_pending_drafts SET {', '.join(sets)} WHERE id = ?"
    with closing(_connect(database_url)) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount > 0


def count_persisted_today(database_url: str, *, account: str) -> int:
    """Count rows persisted for ``account`` since UTC midnight. Used by ζ to
    enforce ``agent.daily_draft_cap`` — defends against a runaway loop on a
    noisy inbox. Counts both ``tier='draft'`` and ``tier='surface'`` rows."""
    with closing(_connect(database_url)) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM agent_pending_drafts
            WHERE account = ? AND date(created_at) = date('now')
            """,
            (account,),
        ).fetchone()
    return int(row[0]) if row else 0


# --- audit log (ε) ---------------------------------------------------------


def log_sweep(
    database_url: str,
    *,
    account: str,
    trigger: str,                          # 'scheduled' | 'manual' | 'api'
    window: str | None,
    threshold: float | None,
    fetched: int,
    kept: int,
    surfaced: int,
    persisted: int,
    errors: list[str] | None,
    standing_instructions_snapshot: str | None,
    started_at: str,                       # ISO timestamp
    finished_at: str,
    duration_ms: int,
) -> int:
    """Append one ``agent_audit`` row for a completed triage sweep."""
    with closing(_connect(database_url)) as conn:
        cur = conn.execute(
            """
            INSERT INTO agent_audit (
                account, trigger, window, threshold,
                fetched, kept, surfaced, persisted,
                errors_json, standing_instructions_snapshot,
                started_at, finished_at, duration_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account, trigger, window, threshold,
                int(fetched), int(kept), int(surfaced), int(persisted),
                json.dumps(errors or [], ensure_ascii=False),
                standing_instructions_snapshot,
                started_at, finished_at, int(duration_ms),
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def list_recent_sweeps(
    database_url: str,
    *,
    account: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return the most-recent sweeps, newest first, with the JSON error list
    rehydrated. Used by the /triage 'Recent activity' panel."""
    sql = "SELECT * FROM agent_audit"
    params: list[Any] = []
    if account:
        sql += " WHERE account = ?"
        params.append(account)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(int(limit))
    with closing(_connect(database_url)) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_audit_row_to_dict(r) for r in rows]


def _audit_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    v = d.get("errors_json")
    if isinstance(v, str):
        try:
            d["errors"] = json.loads(v)
        except json.JSONDecodeError:
            d["errors"] = []
    return d


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    # JSON columns are stored as strings; rehydrate for the API.
    for k in ("reasons_json", "draft_repairs_json"):
        v = d.get(k)
        if isinstance(v, str):
            try:
                d[k.replace("_json", "")] = json.loads(v)
            except json.JSONDecodeError:
                d[k.replace("_json", "")] = []
    d["cold_outreach"] = bool(d.get("cold_outreach", 0))
    return d
