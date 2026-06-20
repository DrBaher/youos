"""Reflect YouOS's queue state into Gmail labels, so the inbox LIST shows which
threads have a draft or a pending calendar invite — at a glance, on web + mobile.

Gmail/Workspace Add-ons can't draw icons in the message list (side-panel +
compose only), so the native way to get a per-thread indicator is a colored
**label** chip. This sync reconciles two YouOS-owned labels against the queue:

* ``YouOS/Drafted``        — a draft is pending for the thread.
* ``YouOS/Invite-Pending`` — a calendar event for the thread awaits approval.

It only ever touches those two labels (never the user's own), and it's a
reversible mailbox mutation gated by ``agent.labels.status_sync`` (default off).
Reconciliation is stateless: desired set (from the DB) vs currently-labelled
(from a ``label:`` search) → add the difference, remove the stragglers. Runs at
the end of a sweep, failure-isolated.
"""

from __future__ import annotations

import logging
from contextlib import closing

from app.agent.store import _connect

logger = logging.getLogger(__name__)

DRAFTED_LABEL = "YouOS/Drafted"
INVITE_LABEL = "YouOS/Invite-Pending"
OWED_LABEL = "YouOS/Follow-up-Owed"
AWAITING_LABEL = "YouOS/Awaiting-Reply"
URGENT_LABEL = "YouOS/Urgent"
NEEDS_REVIEW_LABEL = "YouOS/Needs-Review"

# Urgency cutoff for the Urgent label. The scorer (app/core/urgency.py) weights a
# single strong signal at 0.40–0.45, so 0.5 requires a genuine combination
# (urgent intent / explicit marker / deadline + something) — not just a trailing
# question (0.10), which would over-label.
URGENCY_LABEL_THRESHOLD = 0.5


def _map(rows) -> dict[str, str]:
    return {r["thread_id"]: r["message_id"] for r in rows if r["thread_id"] and r["message_id"]}


def _desired_drafted(database_url: str, account: str) -> dict[str, str]:
    """{thread_id: message_id} for threads with a live (pending/amended) DRAFT."""
    with closing(_connect(database_url)) as conn:
        return _map(conn.execute(
            "SELECT thread_id, message_id FROM agent_pending_drafts "
            "WHERE account = ? AND tier = 'draft' AND status IN ('pending', 'amended')",
            (account,),
        ).fetchall())


def _desired_invite(database_url: str, account: str) -> dict[str, str]:
    """{thread_id: message_id} for threads with a pending calendar event."""
    with closing(_connect(database_url)) as conn:
        return _map(conn.execute(
            "SELECT thread_id, message_id FROM agent_pending_events "
            "WHERE account = ? AND status = 'pending'",
            (account,),
        ).fetchall())


def _desired_needs_review(database_url: str, account: str) -> dict[str, str]:
    """{thread_id: message_id} for surfaced-but-not-drafted (borderline) threads."""
    with closing(_connect(database_url)) as conn:
        return _map(conn.execute(
            "SELECT thread_id, message_id FROM agent_pending_drafts "
            "WHERE account = ? AND status = 'pending' AND tier = 'surface'",
            (account,),
        ).fetchall())


def _desired_urgent(database_url: str, account: str) -> dict[str, str]:
    """{thread_id: message_id} for live pending threads scored time-critical."""
    with closing(_connect(database_url)) as conn:
        return _map(conn.execute(
            "SELECT thread_id, message_id FROM agent_pending_drafts "
            "WHERE account = ? AND status = 'pending' AND urgency_score >= ?",
            (account, URGENCY_LABEL_THRESHOLD),
        ).fetchall())


def _ids_to_map(database_url: str, ids: list[int]) -> dict[str, str]:
    """{thread_id: message_id} for the given pending-draft row ids (used to map
    the followups previews, which carry the row id but not the message id)."""
    if not ids:
        return {}
    qs = ",".join("?" for _ in ids)
    with closing(_connect(database_url)) as conn:
        return _map(conn.execute(
            f"SELECT thread_id, message_id FROM agent_pending_drafts WHERE id IN ({qs})",
            list(ids),
        ).fetchall())


def _desired_owed(database_url: str, account: str) -> dict[str, str]:
    """Inbound you queued but haven't acted on past agent.followup_owed_days."""
    from app.agent import followups

    cfg = followups.get_followup_config()
    items = followups.owed_inbound(database_url, account=account, owed_days=cfg["owed_days"])
    return _ids_to_map(database_url, [i["id"] for i in items])


def _desired_awaiting(database_url: str, account: str) -> dict[str, str]:
    """Replies you pushed/sent with no response past agent.followup_wait_days."""
    from app.agent import followups

    cfg = followups.get_followup_config()
    items = followups.awaiting_reply(database_url, account=account, wait_days=cfg["wait_days"])
    return _ids_to_map(database_url, [i["id"] for i in items])


# The label registry — add a (name, desired-set fn) pair to add a new chip.
def _label_specs():
    return [
        (DRAFTED_LABEL, _desired_drafted),
        (INVITE_LABEL, _desired_invite),
        (OWED_LABEL, _desired_owed),
        (AWAITING_LABEL, _desired_awaiting),
        (URGENT_LABEL, _desired_urgent),
        (NEEDS_REVIEW_LABEL, _desired_needs_review),
    ]


def _current_labelled(account: str, label: str) -> dict[str, list[str]]:
    """{thread_id: [message_id, ...]} currently carrying ``label`` in Gmail."""
    from app.agent.gmail_label_sync import _gog_search_labelled

    out: dict[str, list[str]] = {}
    for m in _gog_search_labelled(account=account, label=label) or []:
        if not isinstance(m, dict):
            continue
        tid = m.get("threadId") or m.get("id")
        mid = m.get("id") or m.get("messageId")
        if tid and mid:
            out.setdefault(tid, []).append(mid)
    return out


def _reconcile_label(
    database_url: str, account: str, label: str, desired: dict[str, str], *, dry_run: bool
) -> dict[str, int]:
    """Add ``label`` to desired threads missing it, remove it from threads that
    no longer qualify. Per-thread failures are isolated (logged, skipped)."""
    from app.ingestion import gmail_write

    gmail_write.ensure_label(account=account, name=label)
    current = _current_labelled(account, label)
    added = removed = errors = 0

    for tid, mid in desired.items():
        if tid in current:
            continue  # already labelled
        try:
            gmail_write.modify_message_labels(account=account, message_id=mid, add=[label], dry_run=dry_run)
            added += 1
        except Exception as exc:  # noqa: BLE001 — one thread must not abort the sync
            errors += 1
            logger.warning("status-label add failed (%s thread=%s): %s", label, tid, exc)

    for tid, mids in current.items():
        if tid in desired:
            continue  # still qualifies
        for mid in mids:  # the label may sit on >1 message of the thread
            try:
                gmail_write.modify_message_labels(account=account, message_id=mid, remove=[label], dry_run=dry_run)
                removed += 1
            except Exception as exc:  # noqa: BLE001
                errors += 1
                logger.warning("status-label remove failed (%s thread=%s): %s", label, tid, exc)

    return {"added": added, "removed": removed, "errors": errors}


def sync_status_labels(database_url: str, account: str, *, dry_run: bool = False) -> dict[str, dict[str, int]]:
    """Reconcile all YouOS status labels for ``account``. Returns per-label
    counts. The caller gates this behind ``agent.labels.status_sync``. A failure
    computing/reconciling one label is isolated so the others still sync."""
    out: dict[str, dict[str, int]] = {}
    for label, desired_fn in _label_specs():
        try:
            out[label] = _reconcile_label(
                database_url, account, label, desired_fn(database_url, account), dry_run=dry_run
            )
        except Exception as exc:  # noqa: BLE001 — one label must not block the rest
            logger.warning("status-label sync failed for %s: %s", label, exc)
            out[label] = {"added": 0, "removed": 0, "errors": 1}
    return out
