"""Capture the agent's OWN draft outcomes as feedback — close the loop.

The corpus the model fine-tunes on is the user's historical sent mail. The
agent's *own* queued drafts — the ones the user dismissed, edited, or kept and
sent — were never fed back, so the live false positives and bad drafts never
became negative signal. This mines the queue lifecycle into ``feedback_pairs``:

* **edited then kept** → a correction pair (generated ``draft`` vs the user's
  ``amended_draft``): the highest-value training signal.
* **sent unchanged** → a strong positive (the draft was good enough as-is).
* **dismissed ``wrong_content``** → a negative pair (this draft missed the
  point). ``noise`` / ``wrong_sender`` dismissals are *classifier* signals, not
  drafting corrections, so they're skipped here (the precision harness owns
  them).

Idempotent via the ``feedback_captured`` marker — each terminal row is mined
exactly once. Read-mostly: it only inserts feedback rows and flips the marker.
"""

from __future__ import annotations

import logging
from contextlib import closing
from typing import Any

logger = logging.getLogger(__name__)


def _edit_distance_pct(generated: str, final: str) -> float:
    from app.core.diff import similarity_ratio

    return round(1.0 - similarity_ratio(generated or "", final or ""), 4)


def _classify_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Map a terminal queue row to a feedback pair, or None to skip it.

    Returns ``{generated, edited, rating, note, edit_distance_pct}``."""
    draft = row.get("draft")
    inbound = row.get("body")
    if not draft or not inbound:
        return None  # nothing to learn from (surface tier / empty)

    status = row.get("status")
    amended = row.get("amended_draft")
    send_state = row.get("send_state")
    reason = row.get("dismissal_reason")

    # Edited before keeping → correction pair (generated vs the user's edit).
    if amended and amended.strip() and amended.strip() != draft.strip():
        ed = _edit_distance_pct(draft, amended)
        return {
            "generated": draft, "edited": amended,
            "rating": 4, "edit_distance_pct": ed,
            "note": "agent-queue: edited before keep",
        }

    # Sent unchanged → strong positive (good enough to send as-is). Two
    # confirmed-send paths count: we auto-sent it (send_state='sent'), OR the
    # user marked it sent themselves — store.mark_sent sets status='sent' and
    # leaves send_state NULL. A pushed-but-unconfirmed Gmail draft
    # (send_state='draft_created') is deliberately NOT counted here: we can't
    # confirm it actually went out, and it's handled as in-flight by the caller.
    sent_confirmed = send_state == "sent" or (status == "sent" and send_state is None)
    if sent_confirmed and not (amended and amended.strip()):
        return {
            "generated": draft, "edited": draft,
            "rating": 5, "edit_distance_pct": 0.0,
            "note": "agent-queue: sent unchanged",
        }

    # Dismissed because the draft missed the point → negative pair.
    if status == "dismissed" and reason == "wrong_content":
        return {
            "generated": draft, "edited": draft,
            "rating": 2, "edit_distance_pct": 0.0,
            "note": "agent-queue: dismissed (wrong_content)",
        }

    # noise / wrong_sender / already_handled / other → not a drafting signal.
    return None


def _in_flight(row: dict[str, Any]) -> bool:
    """A pushed Gmail draft awaiting send-or-dismiss (send_state='draft_created',
    not yet dismissed). Its outcome isn't known yet, so it must NOT be mined or
    burned — a later sweep captures it once it resolves to sent/dismissed."""
    return row.get("send_state") == "draft_created" and row.get("status") != "dismissed"


def capture_queue_feedback(
    database_url: str,
    *,
    limit: int = 500,
) -> dict[str, int]:
    """Mine uncaptured terminal queue rows into ``feedback_pairs``.

    Returns ``{scanned, captured, skipped}``. Every scanned row (captured or
    skipped) is marked ``feedback_captured=1`` so it's never re-mined."""
    from app.agent.store import _connect

    captured = skipped = 0
    with closing(_connect(database_url)) as conn:
        rows = conn.execute(
            "SELECT * FROM agent_pending_drafts "
            "WHERE feedback_captured = 0 "
            "AND (status IN ('sent', 'dismissed', 'amended')) "
            "ORDER BY id ASC LIMIT ?",
            (int(limit),),
        ).fetchall()
        scanned = 0
        for r in rows:
            row = dict(r)
            # Don't burn a pushed draft that hasn't resolved yet — revisit it on
            # a later sweep once it's sent or dismissed, so its outcome isn't
            # lost to the feedback_captured marker.
            if _in_flight(row):
                continue
            scanned += 1
            pair = _classify_row(row)
            if pair is None:
                skipped += 1
            else:
                conn.execute(
                    "INSERT INTO feedback_pairs "
                    "(inbound_text, generated_draft, edited_reply, feedback_note, "
                    " rating, used_in_finetune, edit_distance_pct, organic) "
                    "VALUES (?, ?, ?, ?, ?, 0, ?, 0)",
                    (
                        row.get("body"), pair["generated"], pair["edited"],
                        pair["note"], pair["rating"], pair["edit_distance_pct"],
                    ),
                )
                captured += 1
            conn.execute(
                "UPDATE agent_pending_drafts SET feedback_captured = 1 WHERE id = ?",
                (row["id"],),
            )
        conn.commit()

    if captured:
        logger.info("queue feedback: captured %d pair(s) from %d terminal rows", captured, scanned)
    return {"scanned": scanned, "captured": captured, "skipped": skipped}
