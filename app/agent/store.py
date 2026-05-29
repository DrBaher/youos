"""Persistence for the autonomous-agent loop's triage results.

One row per inbound the agent processed, drafts *and* surface-for-review.
The hard-skipped majority (newsletters, automation, CI mail) is not stored
— it's noise. Idempotent on ``message_id`` so repeated triage runs don't
re-draft the same inbound.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

Tier = Literal["draft", "surface"]
Status = Literal["pending", "amended", "sent", "dismissed"]


def _db_path(database_url: str) -> Path:
    """Parse a ``sqlite:///<path>`` URL to a filesystem path.

    Uses ``removeprefix`` (matching ``app/db/bootstrap.py``) rather than
    ``urllib.parse.urlparse``, because urlparse always treats the parsed
    path as absolute (``sqlite:///var/youos.db`` → ``/var/youos.db``),
    breaking the *relative* path that ``Settings`` emits by default. The
    bootstrap script + ingestion modules all use removeprefix; this aligns.
    """
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError(f"Only sqlite:/// URLs are supported (got {database_url!r})")
    return Path(database_url.removeprefix(prefix))


def _connect(database_url: str) -> sqlite3.Connection:
    # Use the shared, concurrency-tuned connector (busy_timeout + WAL) rather
    # than a raw sqlite3.connect. The agent loop, manual /triage runs, and the
    # nightly pipeline can all write concurrently; without busy_timeout a
    # colliding writer raises 'database is locked' immediately and the sweep
    # silently drops its (expensive) drafts. bootstrap.connect mirrors what the
    # rest of the app already standardized on.
    from app.db.bootstrap import connect

    conn = connect(_db_path(database_url))
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


# --- push-to-Gmail atomic claim (idempotency / concurrency guard) ----------


def begin_push(database_url: str, row_id: int) -> tuple[str, str | None, str | None]:
    """Atomically claim a row for a push-to-Gmail write.

    ``push_to_gmail`` creates a *new* Gmail draft on each backend call, so a
    retry, a double-click, or two concurrent orchestrators acting on the same
    digest would each materialize a duplicate draft in the user's Drafts
    folder. This guards the only path that writes to the real mailbox.

    The single conditional UPDATE is the serialization point: at most one
    caller flips ``pending``/``amended`` → ``sent`` while ``gmail_draft_id``
    is still NULL; every other caller loses the race. Returns
    ``(state, existing_draft_id, prev_status)`` where ``state`` is:

      * ``"missing"``      — no such row
      * ``"already"``      — already pushed; ``existing_draft_id`` is set
      * ``"not_pushable"`` — terminal/unexpected status; ``prev_status`` set
      * ``"race_lost"``    — a concurrent push claimed it first (not yet final)
      * ``"claimed"``      — caller won; ``prev_status`` is kept for rollback

    On ``"claimed"`` the caller MUST follow with :func:`finalize_push` on a
    successful write or :func:`abort_push` (with ``prev_status``) on failure.
    """
    with closing(_connect(database_url)) as conn:
        row = conn.execute(
            "SELECT status, gmail_draft_id FROM agent_pending_drafts WHERE id = ?",
            (row_id,),
        ).fetchone()
        if row is None:
            return ("missing", None, None)
        if row["gmail_draft_id"]:
            return ("already", row["gmail_draft_id"], row["status"])
        prev = row["status"]
        if prev not in ("pending", "amended"):
            return ("not_pushable", None, prev)
        cur = conn.execute(
            """UPDATE agent_pending_drafts
               SET status = 'sent', sent_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND status = ? AND gmail_draft_id IS NULL""",
            (row_id, prev),
        )
        conn.commit()
        if cur.rowcount == 0:
            # Lost the race: re-read to see if the winner already finalized.
            r2 = conn.execute(
                "SELECT gmail_draft_id FROM agent_pending_drafts WHERE id = ?",
                (row_id,),
            ).fetchone()
            if r2 and r2["gmail_draft_id"]:
                return ("already", r2["gmail_draft_id"], prev)
            return ("race_lost", None, prev)
        return ("claimed", None, prev)


def finalize_push(database_url: str, row_id: int, *, gmail_draft_id: str) -> None:
    """Record the Gmail draft id after a successful write (status already
    flipped to ``sent`` by :func:`begin_push`)."""
    with closing(_connect(database_url)) as conn:
        conn.execute(
            "UPDATE agent_pending_drafts SET gmail_draft_id = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (gmail_draft_id, row_id),
        )
        conn.commit()


def abort_push(database_url: str, row_id: int, *, prev_status: str) -> None:
    """Roll back a claimed-but-failed push: restore the prior status and clear
    ``sent_at`` so the user can retry. No-op if a draft id slipped in
    (shouldn't happen, but stays safe under concurrency)."""
    with closing(_connect(database_url)) as conn:
        conn.execute(
            """UPDATE agent_pending_drafts
               SET status = ?, sent_at = NULL, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND gmail_draft_id IS NULL""",
            (prev_status, row_id),
        )
        conn.commit()


# Recognised dismissal-reason buckets — kept here (not in the DB) so the API
# can validate and so the tuning code can iterate them without re-querying.
# 'noise' is the only one that's a direct filter-quality signal; the others
# are kept distinct so we don't conflate "we shouldn't have drafted" (filter
# bug) with "right idea, wrong wording" (drafting bug) or "I already replied
# outside YouOS" (orthogonal to both).
DISMISSAL_REASONS: tuple[str, ...] = (
    "noise",
    "wrong_sender",
    "wrong_content",
    "already_handled",
    "other",
)


def mark_dismissed(
    database_url: str,
    row_id: int,
    *,
    reason: str | None = None,
) -> bool:
    """Mark a pending row as dismissed. ``reason`` is one of ``DISMISSAL_REASONS``
    (or ``None`` if the user didn't supply one — legacy callers / older UI).
    Unknown reasons are coerced to ``'other'`` so the column stays bounded —
    the API layer validates and rejects upstream, this is just defence in depth.
    """
    if reason is not None and reason not in DISMISSAL_REASONS:
        reason = "other"
    return _update_status(
        database_url, row_id,
        status="dismissed", dismissed_at_now=True,
        dismissal_reason=reason,
    )


def _update_status(
    database_url: str,
    row_id: int,
    *,
    status: Status,
    amended_draft: str | None = None,
    sent_at_now: bool = False,
    dismissed_at_now: bool = False,
    gmail_draft_id: str | None = None,
    dismissal_reason: str | None = None,
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
    if dismissal_reason is not None:
        sets.append("dismissal_reason = ?")
        params.append(dismissal_reason)
    params.append(row_id)
    sql = f"UPDATE agent_pending_drafts SET {', '.join(sets)} WHERE id = ?"
    with closing(_connect(database_url)) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount > 0


def dismissal_stats(
    database_url: str,
    *,
    account: str | None = None,
    days: int = 30,
) -> dict[str, Any]:
    """Aggregate dismissal signal over a rolling window.

    Returns ``{total_persisted, dismissed, dismissal_rate, by_reason}``
    over the last ``days`` days (UTC). ``by_reason`` is a dict mapping each
    bucket in ``DISMISSAL_REASONS`` to a count (zero-filled), plus a
    ``'no_reason'`` slot for legacy dismissals predating this PR. Used by
    the upcoming agent-observability surface to tell the user "you dismiss
    23% of drafted items, mostly as 'noise' — consider raising the
    threshold or extending skip_senders."
    """
    where = "date(created_at) >= date('now', ?)"
    params: list[Any] = [f"-{int(days)} days"]
    if account:
        where += " AND account = ?"
        params.append(account)

    with closing(_connect(database_url)) as conn:
        total = int(conn.execute(
            f"SELECT COUNT(*) FROM agent_pending_drafts WHERE {where}",
            params,
        ).fetchone()[0])
        dismissed = int(conn.execute(
            f"SELECT COUNT(*) FROM agent_pending_drafts WHERE {where} AND status = 'dismissed'",
            params,
        ).fetchone()[0])
        rows = conn.execute(
            f"""
            SELECT COALESCE(dismissal_reason, 'no_reason') AS r, COUNT(*) AS c
            FROM agent_pending_drafts
            WHERE {where} AND status = 'dismissed'
            GROUP BY r
            """,
            params,
        ).fetchall()

    by_reason: dict[str, int] = {r: 0 for r in DISMISSAL_REASONS}
    by_reason["no_reason"] = 0
    for row in rows:
        by_reason[row["r"]] = int(row["c"])

    rate = (dismissed / total) if total else 0.0
    return {
        "total_persisted": total,
        "dismissed": dismissed,
        "dismissal_rate": round(rate, 4),
        "by_reason": by_reason,
        "window_days": int(days),
        "account": account,
    }


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
    auto_promoted_senders: list[str] | None = None,
) -> int:
    """Append one ``agent_audit`` row for a completed triage sweep.

    ``auto_promoted_senders`` (b52) is the list of senders the loop just
    auto-added to ``agent.skip_senders`` at the tail of this sweep — empty
    when ``agent.auto_promote_skip_senders`` is off or no candidates
    qualified. Surfaces in /triage Recent activity.
    """
    with closing(_connect(database_url)) as conn:
        cur = conn.execute(
            """
            INSERT INTO agent_audit (
                account, trigger, window, threshold,
                fetched, kept, surfaced, persisted,
                errors_json, standing_instructions_snapshot,
                started_at, finished_at, duration_ms,
                auto_promoted_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account, trigger, window, threshold,
                int(fetched), int(kept), int(surfaced), int(persisted),
                json.dumps(errors or [], ensure_ascii=False),
                standing_instructions_snapshot,
                started_at, finished_at, int(duration_ms),
                json.dumps(auto_promoted_senders or [], ensure_ascii=False),
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


def sweep_aggregate(
    database_url: str,
    *,
    account: str | None = None,
    days: int = 30,
) -> dict[str, Any]:
    """Roll up the audit log over a window — drives the observability card.

    Returns sweep totals (count, success_count, success_rate, fetched, kept,
    surfaced, persisted) over the last ``days`` days. ``hard_skipped`` is
    derived (``fetched - kept``) since rows that don't survive the hard-skip
    filter aren't persisted — the audit counters are the only place we
    record them.

    A sweep is "successful" iff its ``errors_json`` is empty. A sweep that
    fetched 0 mail (idle inbox) still counts as successful.
    """
    where = "started_at >= datetime('now', ?)"
    params: list[Any] = [f"-{int(days)} days"]
    if account:
        where += " AND account = ?"
        params.append(account)

    with closing(_connect(database_url)) as conn:
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS sweeps,
                SUM(CASE WHEN errors_json IN ('[]','') OR errors_json IS NULL THEN 1 ELSE 0 END) AS ok,
                COALESCE(SUM(fetched), 0)   AS fetched,
                COALESCE(SUM(kept), 0)      AS kept,
                COALESCE(SUM(surfaced), 0)  AS surfaced,
                COALESCE(SUM(persisted), 0) AS persisted,
                COALESCE(AVG(duration_ms), 0) AS avg_ms,
                MAX(started_at) AS last_sweep,
                MAX(CASE WHEN errors_json IN ('[]','') OR errors_json IS NULL
                         THEN started_at END) AS last_ok_sweep,
                CAST((julianday('now') - julianday(MAX(started_at))) * 86400 AS INTEGER) AS secs_since
            FROM agent_audit WHERE {where}
            """,
            params,
        ).fetchone()

    sweeps = int(row["sweeps"] or 0)
    ok = int(row["ok"] or 0)
    fetched = int(row["fetched"] or 0)
    kept = int(row["kept"] or 0)
    secs_since = row["secs_since"]
    return {
        "sweeps": sweeps,
        "successful": ok,
        "success_rate": round(ok / sweeps, 4) if sweeps else 0.0,
        "fetched": fetched,
        "hard_skipped": max(fetched - kept, 0),
        "kept": kept,
        "surfaced": int(row["surfaced"] or 0),
        "persisted": int(row["persisted"] or 0),
        "avg_duration_ms": int(row["avg_ms"] or 0),
        # Heartbeat: lets the user / orchestrator answer "is the agent still
        # alive and on-schedule?" — there was no positive liveness signal
        # before, only the absence of new drafts.
        "last_sweep_at": row["last_sweep"],
        "last_successful_sweep_at": row["last_ok_sweep"],
        "seconds_since_last_sweep": int(secs_since) if secs_since is not None else None,
        "window_days": int(days),
        "account": account,
    }


def score_histogram(
    database_url: str,
    *,
    account: str | None = None,
    days: int = 30,
) -> dict[str, Any]:
    """Distribution of needs_reply scores across persisted rows.

    Buckets the score column into 0.0-0.3 / 0.3-0.5 / 0.5-0.7 / 0.7-0.9 /
    0.9-1.0 so the observability card can show "your filter mostly draws
    rows from the 0.7-0.9 range — looks healthy" or "everything's clustering
    at 0.6 — your threshold is right on the boundary." The boundary choices
    line up with the surface-for-review band (0.30–0.59).
    """
    where = "date(created_at) >= date('now', ?)"
    params: list[Any] = [f"-{int(days)} days"]
    if account:
        where += " AND account = ?"
        params.append(account)

    buckets = [
        ("0.0-0.3", 0.0, 0.3),
        ("0.3-0.5", 0.3, 0.5),
        ("0.5-0.7", 0.5, 0.7),
        ("0.7-0.9", 0.7, 0.9),
        ("0.9-1.0", 0.9, 1.01),    # 1.01 so a row at exactly 1.0 lands here
    ]
    with closing(_connect(database_url)) as conn:
        out: dict[str, int] = {}
        for label, lo, hi in buckets:
            cnt = conn.execute(
                f"""SELECT COUNT(*) FROM agent_pending_drafts
                    WHERE {where} AND needs_reply_score >= ? AND needs_reply_score < ?""",
                params + [lo, hi],
            ).fetchone()[0]
            out[label] = int(cnt)
    return {"buckets": out, "window_days": int(days), "account": account}


def noise_dismissal_candidates(
    database_url: str,
    *,
    account: str | None = None,
    days: int = 30,
    min_count: int = 2,
) -> list[dict[str, Any]]:
    """Senders the user has dismissed as 'noise' ``min_count`` or more times.

    Drives the skip-sender promotion UI on /triage: when the same sender
    keeps slipping past the filter and the user keeps dismissing them as
    noise, the right answer is to add them to ``agent.skip_senders`` — but
    we can't do that automatically without an explicit signal. This helper
    finds the candidates; the UI lets the user promote them in one click.

    Returns one row per sender_email, with ``count`` (number of noise
    dismissals in the window) and ``last_subject`` (most recent dismissed
    subject, as a memory aid). Ordered by count DESC then most-recent.
    Excludes rows where ``sender_email`` is NULL — without an email
    address there's nothing to add to the skip list.
    """
    where = "date(created_at) >= date('now', ?) AND status = 'dismissed' " \
            "AND dismissal_reason = 'noise' AND sender_email IS NOT NULL " \
            "AND sender_email != ''"
    params: list[Any] = [f"-{int(days)} days"]
    if account:
        where += " AND account = ?"
        params.append(account)

    with closing(_connect(database_url)) as conn:
        rows = conn.execute(
            f"""
            SELECT
                LOWER(sender_email) AS sender_email,
                COUNT(*)            AS cnt,
                MAX(created_at)     AS most_recent,
                (SELECT subject FROM agent_pending_drafts a2
                   WHERE LOWER(a2.sender_email) = LOWER(agent_pending_drafts.sender_email)
                     AND a2.status = 'dismissed' AND a2.dismissal_reason = 'noise'
                   ORDER BY a2.created_at DESC LIMIT 1) AS last_subject
            FROM agent_pending_drafts
            WHERE {where}
            GROUP BY LOWER(sender_email)
            HAVING cnt >= ?
            ORDER BY cnt DESC, most_recent DESC
            """,
            params + [int(min_count)],
        ).fetchall()

    return [
        {
            "sender_email": r["sender_email"],
            "count": int(r["cnt"]),
            "most_recent": r["most_recent"],
            "last_subject": r["last_subject"],
        }
        for r in rows
    ]


def _audit_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for src, dst in (("errors_json", "errors"), ("auto_promoted_json", "auto_promoted")):
        v = d.get(src)
        if isinstance(v, str):
            try:
                d[dst] = json.loads(v)
            except json.JSONDecodeError:
                d[dst] = []
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
