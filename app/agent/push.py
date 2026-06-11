"""Shared push-to-Gmail-Drafts helper.

One code path for materializing a pending agent draft as a real Gmail Draft,
used by both the manual ``POST /api/agent/pending/{id}/push_to_gmail`` route
and the (opt-in) tiered auto-push in the triage loop. Keeping it in one place
is deliberate: the route and the autonomous path must share the exact same
idempotency guard so neither can produce duplicate drafts.

**Never sends.** Creates a draft on the original thread; the human (or, for
auto-push, the human reviewing their Drafts folder) finishes-and-sends.

The duplicate-draft hazard is real: each backend ``create_draft`` call makes a
*new* Gmail draft, so a retry after a timeout, a double-click, or two
orchestrators acting on the same digest would each leave a copy. The atomic
claim in :func:`app.agent.store.begin_push` serializes the write so at most one
caller proceeds; everyone else gets the existing draft id back (idempotent).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.agent import store

logger = logging.getLogger(__name__)


def _include_signature() -> bool:
    """``generation.push.include_signature`` — default True. Append the user's
    Gmail signature to pushed drafts."""
    try:
        from app.core.config import load_config

        gen = (load_config() or {}).get("generation", {})
        push = gen.get("push", {}) if isinstance(gen, dict) else {}
        return push.get("include_signature", True) is not False
    except Exception:
        return True


def _reply_all_cc(
    *, sender_email: str, to_recipients: str | None, cc_recipients: str | None
) -> str | None:
    """Cc string for a reply-all: every address the original thread put in To or
    Cc, minus the user's own addresses and the sender (who's the To of the
    reply). Preserves the loop instead of dropping the Cc list. Returns None when
    there's no one left to copy (a plain 1:1 reply)."""
    from app.agent.needs_reply import _header_emails
    from app.core.config import get_user_emails

    mine = {e.lower() for e in get_user_emails() if e}
    drop = mine | {(sender_email or "").lower()}
    others = (_header_emails(to_recipients) | _header_emails(cc_recipients)) - drop
    return ", ".join(sorted(others)) or None


@dataclass
class PushOutcome:
    """Result of a push attempt. ``ok`` distinguishes success from a handled
    failure; on failure ``http_status``/``detail`` carry an actionable message
    the route maps straight onto an ``HTTPException``."""

    ok: bool
    gmail_draft_id: str | None = None
    pushed_already: bool = False
    row: dict | None = None
    http_status: int | None = None
    detail: str | None = None


def push_pending_row(database_url: str, row_id: int, *, backend: str | None = None) -> PushOutcome:
    """Create a Gmail draft for the pending row, idempotently.

    Validates the row, atomically claims it, performs the backend write, and
    records the resulting Gmail draft id. If the row was already pushed,
    returns the existing draft id with ``pushed_already=True`` (HTTP 200) — no
    second draft is created. Backend errors roll the claim back so the user can
    retry without the row being stuck in ``sent``.
    """
    row = store.get(database_url, row_id)
    if not row:
        return PushOutcome(False, http_status=404, detail="pending row not found")
    if row.get("tier") != "draft" or not (row.get("amended_draft") or row.get("draft")):
        return PushOutcome(
            False, http_status=400,
            detail="row has no draft to push (tier=surface, or draft is empty)",
        )
    if not row.get("sender_email"):
        return PushOutcome(
            False, http_status=400,
            detail="row has no sender_email; cannot route the reply",
        )

    # Fast idempotent path: already pushed — hand back the existing draft id
    # without touching Gmail again.
    if row.get("gmail_draft_id"):
        return PushOutcome(
            True, gmail_draft_id=row["gmail_draft_id"], pushed_already=True, row=row,
        )

    state, existing_id, prev = store.begin_push(database_url, row_id)
    if state == "missing":
        return PushOutcome(False, http_status=404, detail="pending row not found")
    if state == "already":
        return PushOutcome(
            True, gmail_draft_id=existing_id, pushed_already=True,
            row=store.get(database_url, row_id),
        )
    if state == "not_pushable":
        return PushOutcome(
            False, http_status=409,
            detail=f"row status is {prev!r}; only pending/amended rows can be pushed",
        )
    if state == "race_lost":
        return PushOutcome(
            False, http_status=409,
            detail="a push for this row is already in progress; retry shortly",
        )

    # state == "claimed": we own the write. prev holds the status to restore on
    # failure. Import gmail_write lazily and call through the module so tests
    # that monkeypatch gmail_write.create_draft take effect.
    from app.ingestion import gmail_write

    body = row.get("amended_draft") or row.get("draft") or ""
    raw_subject = row.get("subject") or ""
    # Outreach rows (b232) are NEW outbound mail to a lead-form prospect, not
    # replies: subject goes out as-is (no "Re:"), there is no thread to attach
    # to, and nobody to reply-all.
    is_outreach = bool(row.get("outreach"))
    if is_outreach:
        subject = raw_subject
        cc = None
    else:
        subject = raw_subject if raw_subject.lower().startswith("re:") else f"Re: {raw_subject}"

        # Reply-all by default: keep everyone the original thread addressed (its
        # To + Cc) in Cc, minus you and the person you're replying to (who goes
        # in To). A plain reply-to-sender would silently drop the Cc list. Uses
        # the To/Cc persisted on the row (b213); falls back to a bare reply when
        # absent.
        cc = _reply_all_cc(
            sender_email=row["sender_email"],
            to_recipients=row.get("to_recipients"),
            cc_recipients=row.get("cc_recipients"),
        )

    # Always include the user's Gmail signature (the API, unlike the web
    # composer, won't append it). Best-effort: a fetch failure pushes without it
    # rather than blocking. Disable with generation.push.include_signature: false.
    signature_html = None
    if _include_signature():
        try:
            signature_html = gmail_write.get_signature(account=row["account"], backend=backend) or None
        except Exception:
            logger.debug("signature fetch failed; pushing without it", exc_info=True)

    try:
        result = gmail_write.create_draft(
            account=row["account"],
            reply_to_message_id=None if is_outreach else row.get("message_id"),
            thread_id=None if is_outreach else row.get("thread_id"),
            to_email=row["sender_email"],
            subject=subject,
            body=body,
            cc=cc,
            signature_html=signature_html,
            backend=backend,
        )
    except NotImplementedError as exc:
        store.abort_push(database_url, row_id, prev_status=prev or "pending")
        return PushOutcome(False, http_status=501, detail=str(exc))
    except gmail_write.GmailWriteError as exc:
        store.abort_push(database_url, row_id, prev_status=prev or "pending")
        return PushOutcome(False, http_status=502, detail=f"Gmail write failed: {exc}")
    except Exception as exc:  # noqa: BLE001 — never leave a claimed row orphaned
        store.abort_push(database_url, row_id, prev_status=prev or "pending")
        logger.warning("push_pending_row: unexpected error for row %s: %s", row_id, exc)
        return PushOutcome(False, http_status=500, detail=f"push failed: {exc}")

    store.finalize_push(database_url, row_id, gmail_draft_id=result.draft_id)
    return PushOutcome(
        True, gmail_draft_id=result.draft_id, row=store.get(database_url, row_id),
    )
