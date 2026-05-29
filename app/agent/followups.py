"""Follow-up tracking — the two open loops a real assistant never drops.

1. **Owed inbound**: mail the agent queued (``status='pending'``) that you
   haven't acted on, aging past ``agent.followup_owed_days``. "Bob's email from
   Tuesday is still unanswered."
2. **Awaiting reply**: replies you pushed/sent (``status='sent'``, a
   ``gmail_draft_id`` is set) where nothing newer has arrived on the thread
   after ``agent.followup_wait_days``. "You emailed Alice 4 days ago, no reply —
   want a nudge?"

Both are read-only surfacing over the existing ``agent_pending_drafts`` table —
no new writes, no Gmail egress, fully inside the trust boundary. Timestamps are
parsed in Python (not SQLite ``julianday``) because ``received_at`` carries
email-style ISO strings — sometimes with a ``Z`` suffix SQLite's date functions
reject — while ``created_at`` is SQLite's space-separated UTC format.

The awaiting-reply check is a DB-only heuristic: it infers "they replied" from a
newer row appearing on the same thread in a later sweep. It can miss a reply the
agent never re-swept; treat the nudge as a soft suggestion, not a guarantee.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_OWED_DAYS = 2
DEFAULT_WAIT_DAYS = 4


def _db_path(database_url: str) -> Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError(f"Only sqlite:/// URLs are supported (got {database_url!r})")
    return Path(database_url.removeprefix(prefix))


def _connect(database_url: str) -> sqlite3.Connection:
    from app.db.bootstrap import connect

    conn = connect(_db_path(database_url))
    conn.row_factory = sqlite3.Row
    return conn


def _age_days(ts: str | None, *, now: datetime | None = None) -> float | None:
    """Age in days of an ISO/SQLite timestamp, or None if unparseable.

    Tolerates ``...Z`` (email ISO), ``+00:00`` offsets, and SQLite's
    ``YYYY-MM-DD HH:MM:SS`` (assumed UTC). Naive timestamps are treated as UTC.
    """
    if not ts:
        return None
    s = str(ts).strip().replace("Z", "+00:00")
    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - dt).total_seconds() / 86400.0


def get_followup_config() -> dict[str, int]:
    """Read ``agent.followup_*`` day thresholds (safe defaults)."""
    try:
        from app.core.config import load_config

        cfg = load_config() or {}
        a = (cfg.get("agent") or {}) if isinstance(cfg, dict) else {}
        if not isinstance(a, dict):
            a = {}
        owed = int(a.get("followup_owed_days", DEFAULT_OWED_DAYS) or DEFAULT_OWED_DAYS)
        wait = int(a.get("followup_wait_days", DEFAULT_WAIT_DAYS) or DEFAULT_WAIT_DAYS)
    except Exception:
        owed, wait = DEFAULT_OWED_DAYS, DEFAULT_WAIT_DAYS
    return {"owed_days": max(0, owed), "wait_days": max(0, wait)}


def _preview(row: sqlite3.Row, age: float) -> dict[str, Any]:
    return {
        "id": row["id"],
        "subject": row["subject"],
        "sender": row["sender"],
        "sender_email": row["sender_email"],
        "thread_id": row["thread_id"],
        "age_days": round(age, 1),
    }


def owed_inbound(
    database_url: str,
    *,
    account: str | None = None,
    owed_days: int = DEFAULT_OWED_DAYS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Pending rows (drafted or surfaced, not yet acted on) older than
    ``owed_days``, by inbound ``received_at`` (falling back to ``created_at``).
    Oldest first — the most overdue surface at the top."""
    sql = "SELECT * FROM agent_pending_drafts WHERE status = 'pending'"
    params: list[Any] = []
    if account:
        sql += " AND account = ?"
        params.append(account)
    with closing(_connect(database_url)) as conn:
        rows = conn.execute(sql, params).fetchall()

    out: list[tuple[float, dict[str, Any]]] = []
    for r in rows:
        age = _age_days(r["received_at"], now=now)
        if age is None:
            age = _age_days(r["created_at"], now=now)
        if age is not None and age >= owed_days:
            out.append((age, _preview(r, age)))
    out.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in out]


def awaiting_reply(
    database_url: str,
    *,
    account: str | None = None,
    wait_days: int = DEFAULT_WAIT_DAYS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Sent replies (a Gmail draft was created) older than ``wait_days`` with no
    newer activity on the thread — i.e. you're probably still waiting on them.

    Heuristic: "newer activity" = any row for the same account+thread with a
    ``created_at`` after this row's ``sent_at``. A reply the agent never swept
    won't be detected; treat as a soft nudge."""
    sql = (
        "SELECT * FROM agent_pending_drafts "
        "WHERE status = 'sent' AND gmail_draft_id IS NOT NULL"
    )
    params: list[Any] = []
    if account:
        sql += " AND account = ?"
        params.append(account)
    with closing(_connect(database_url)) as conn:
        rows = conn.execute(sql, params).fetchall()

        out: list[tuple[float, dict[str, Any]]] = []
        for r in rows:
            age = _age_days(r["sent_at"], now=now)
            if age is None or age < wait_days:
                continue
            # Did anything newer land on this thread (a reply that got swept)?
            newer = conn.execute(
                """SELECT 1 FROM agent_pending_drafts
                   WHERE account = ? AND thread_id = ? AND id != ?
                     AND created_at > COALESCE(?, created_at)
                   LIMIT 1""",
                (r["account"], r["thread_id"], r["id"], r["sent_at"]),
            ).fetchone()
            if newer:
                continue
            out.append((age, _preview(r, age)))
    out.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in out]


def build_followups(
    database_url: str,
    *,
    account: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Both open loops + counts, for the API and digest."""
    cfg = get_followup_config()
    owed = owed_inbound(database_url, account=account, owed_days=cfg["owed_days"], now=now)
    awaiting = awaiting_reply(database_url, account=account, wait_days=cfg["wait_days"], now=now)
    return {
        "owed": owed,
        "awaiting": awaiting,
        "owed_count": len(owed),
        "awaiting_count": len(awaiting),
        "owed_days": cfg["owed_days"],
        "wait_days": cfg["wait_days"],
        "account": account,
    }
