"""Close the loop with the user's REAL Gmail sends.

YouOS drafts a reply and queues it; the user often replies directly in Gmail,
ignoring the draft. This pairs the YouOS draft with the reply the user
*actually* sent on that thread — the highest-fidelity training signal (real
edit pairs) and the basis for issue-spotting + calibration:

* a user reply found on the thread → a ``(inbound → your_sent)`` feedback pair
  whose ``generated_draft`` is the YouOS draft, with the edit distance between
  them. This is what the nightly LoRA learns from, and high-divergence pairs are
  the "drafts that missed" issue list.
* no reply after the window → an honest ``no_send`` outcome (you didn't reply —
  a needs-reply calibration signal), recorded WITHOUT a training pair.

Matched by **thread id** (not fragile inbound-text equality, which never
joined). Idempotent via the per-row ``outcome_captured`` marker, so each queued
draft is reconciled against your sent mail exactly once.
"""

from __future__ import annotations

import logging
from contextlib import closing
from typing import Any

from app.agent.store import _connect

logger = logging.getLogger(__name__)

# Minimum new-content length (chars) for a sent reply to count as a real reply
# worth pairing — guards against "ok"/"thanks" one-liners and quote-only bodies.
_MIN_REPLY_CHARS = 12


def _edit_distance_pct(generated: str, final: str) -> float:
    from app.core.diff import similarity_ratio

    return round(1.0 - similarity_ratio(generated or "", final or ""), 4)


def _rating_for(ed: float) -> int:
    """A coarse 1–5 rating from the edit distance: draft≈sent → high (the draft
    was good), big divergence → low (the draft missed what you'd say)."""
    if ed < 0.10:
        return 5
    if ed < 0.30:
        return 4
    if ed < 0.60:
        return 3
    return 2


def _user_reply_text(thread: dict[str, Any], *, inbound_message_id: str, user_emails: set[str]) -> str | None:
    """The new content of the FIRST message the user sent after the inbound the
    draft replied to. None when the user hasn't replied on this thread yet."""
    from app.agent.inbox_fetch import _decode_mime_words, _extract_text, _header
    from app.core.sender import extract_email
    from app.core.text_utils import extract_new_content, strip_signature

    messages = thread.get("messages") if isinstance(thread, dict) else None
    if not isinstance(messages, list) or not messages:
        return None

    # Gmail returns thread messages chronologically. Start looking after the
    # inbound the draft replied to; if we can't find it, scan the whole thread.
    start = 0
    for i, m in enumerate(messages):
        if (m.get("id") or m.get("messageId")) == inbound_message_id:
            start = i + 1
            break

    for m in messages[start:]:
        payload = m.get("payload", {}) or {}
        frm = extract_email(_decode_mime_words(_header(payload, "From")))
        if not frm or frm.lower() not in user_emails:
            continue
        text = strip_signature(extract_new_content(_extract_text(payload))).strip()
        if len(text) >= _MIN_REPLY_CHARS:
            return text
    return None


def capture_send_outcomes(
    database_url: str,
    *,
    account: str,
    backend: str | None = None,
    lookback_days: int = 21,
    no_send_after_days: int = 5,
    limit: int = 300,
) -> dict[str, Any]:
    """Reconcile queued YouOS drafts against the user's real Gmail sends.

    For each not-yet-reconciled ``draft``-tier row for ``account`` created within
    ``lookback_days``: fetch its thread, and if the user has since sent a reply,
    store a ``(inbound, youos_draft, your_sent)`` feedback pair and mark the row
    ``outcome='sent'``. If no reply and the row is older than
    ``no_send_after_days``, mark ``outcome='no_send'`` (calibration signal, no
    pair). Otherwise leave it for a later run. Returns a summary used for the
    issues/calibration view.
    """
    from app.core.config import get_user_emails
    from app.ingestion.adapters import get_google_source

    user_emails = {(account or "").lower()} | {e.lower() for e in get_user_emails() if e}
    user_emails.discard("")

    summary: dict[str, Any] = {
        "account": account, "scanned": 0, "paired": 0, "no_send": 0,
        "still_pending": 0, "errors": 0, "avg_edit_distance": None,
        "high_divergence": 0,  # pairs the draft got materially wrong (ed >= 0.6)
    }
    eds: list[float] = []

    with closing(_connect(database_url)) as conn:
        rows = conn.execute(
            """
            SELECT id, message_id, thread_id, account, body, subject, sender, sender_email,
                   draft, amended_draft, created_at,
                   CAST((julianday('now') - julianday(created_at)) AS REAL) AS age_days
            FROM agent_pending_drafts
            WHERE account = ?
              AND tier = 'draft'
              AND draft IS NOT NULL AND draft <> ''
              AND COALESCE(outcome_captured, 0) = 0
              AND created_at >= datetime('now', ?)
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (account, f"-{int(lookback_days)} days", int(limit)),
        ).fetchall()

        source = get_google_source(backend)
        for r in rows:
            summary["scanned"] += 1
            reply = None
            try:
                thread = source.get_thread(account=account, thread_id=r["thread_id"])
                reply = _user_reply_text(
                    thread, inbound_message_id=r["message_id"], user_emails=user_emails
                )
            except Exception:
                summary["errors"] += 1
                logger.debug("outcome fetch failed for thread=%s", r["thread_id"], exc_info=True)
                continue

            if reply:
                generated = r["draft"] or ""
                ed = _edit_distance_pct(generated, reply)
                eds.append(ed)
                if ed >= 0.60:
                    summary["high_divergence"] += 1
                try:
                    from app.core.sender import classify_sender

                    sender_type = classify_sender(r["sender"], database_url)
                except Exception:
                    sender_type = None
                conn.execute(
                    "INSERT INTO feedback_pairs "
                    "(inbound_text, generated_draft, edited_reply, feedback_note, "
                    " rating, used_in_finetune, edit_distance_pct, organic, sender_type) "
                    "VALUES (?, ?, ?, ?, ?, 0, ?, 0, ?)",
                    (
                        r["body"] or "", generated, reply,
                        "auto: real Gmail send outcome (draft vs what you actually sent)",
                        _rating_for(ed), ed, sender_type,
                    ),
                )
                conn.execute(
                    "UPDATE agent_pending_drafts SET outcome_captured = 1, outcome = 'sent' WHERE id = ?",
                    (r["id"],),
                )
                summary["paired"] += 1
            elif (r["age_days"] or 0) >= no_send_after_days:
                conn.execute(
                    "UPDATE agent_pending_drafts SET outcome_captured = 1, outcome = 'no_send' WHERE id = ?",
                    (r["id"],),
                )
                summary["no_send"] += 1
            else:
                summary["still_pending"] += 1
        conn.commit()

    if eds:
        summary["avg_edit_distance"] = round(sum(eds) / len(eds), 4)
    return summary
