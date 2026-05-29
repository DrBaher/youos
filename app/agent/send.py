"""The send frontier — actually sending a queued draft, hard-gated off.

This is the one place that crosses the never-send boundary, and it does so only
behind explicit, default-off gates:

* ``agent.send.enabled`` — master programmatic-send switch (default **false**).
* ``agent.outbound_kill_switch`` — when **true**, blocks every send regardless
  of any other flag. A single switch to stop all outbound instantly.

A send is valid only on a row that already has a Gmail draft (it sends the exact
draft the user could have reviewed — no body re-marshaling). ``shadow=True``
runs the full path but records a ``'shadow'`` send without touching Gmail — the
soak mode the policy ladder uses before any real send is trusted.

Autonomous auto-send (in the sweep, behind ``agent.auto_send.enabled`` + the
confidence×stakes gates + a delay/undo window) builds on this primitive; this
module performs no draft-quality judgement of its own — callers do.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.agent import store

logger = logging.getLogger(__name__)


@dataclass
class SendOutcome:
    ok: bool
    sent_message_id: str | None = None
    shadow: bool = False
    sent_already: bool = False
    row: dict | None = None
    http_status: int | None = None
    detail: str | None = None


def _send_config() -> dict[str, bool]:
    """Read the two send gates. Both default to the safe value (send disabled,
    kill-switch off-but-irrelevant-when-disabled)."""
    from app.core.config import load_config

    cfg = load_config() or {}
    a = (cfg.get("agent") or {}) if isinstance(cfg, dict) else {}
    s = (a.get("send") or {}) if isinstance(a, dict) else {}
    enabled = bool(s.get("enabled", False)) if isinstance(s, dict) else False
    kill = bool(a.get("outbound_kill_switch", False)) if isinstance(a, dict) else False
    return {"enabled": enabled, "kill_switch": kill}


def send_pending_row(
    database_url: str,
    row_id: int,
    *,
    shadow: bool = False,
    dry_run: bool = False,
    backend: str | None = None,
) -> SendOutcome:
    """Send the Gmail draft attached to a pending row, idempotently and gated.

    Gating (checked here, before any Gmail call):
      * the kill-switch blocks everything;
      * ``agent.send.enabled`` must be true for a *real* send. A ``shadow`` send
        (soak) is allowed when sending is disabled — it never touches Gmail.

    The row must already be pushed (have a ``gmail_draft_id``). On success the
    honest ``send_state`` becomes ``'sent'`` (or ``'shadow'``).
    """
    cfg = _send_config()
    if cfg["kill_switch"]:
        return SendOutcome(False, http_status=403, detail="outbound kill-switch is on; all sending is blocked")
    if not shadow and not cfg["enabled"]:
        return SendOutcome(
            False, http_status=403,
            detail="sending is disabled (set agent.send.enabled to allow it; or use shadow mode)",
        )

    row = store.get(database_url, row_id)
    if not row:
        return SendOutcome(False, http_status=404, detail="pending row not found")
    if not row.get("gmail_draft_id"):
        return SendOutcome(
            False, http_status=409,
            detail="row has no Gmail draft to send — push it to Gmail first",
        )
    if row.get("send_state") in ("sent", "shadow"):
        return SendOutcome(
            True, sent_message_id=row.get("sent_message_id"), sent_already=True,
            shadow=row.get("send_state") == "shadow", row=row,
        )

    state, draft_id = store.begin_send(database_url, row_id)
    if state == "missing":
        return SendOutcome(False, http_status=404, detail="pending row not found")
    if state == "not_pushed":
        return SendOutcome(False, http_status=409, detail="row has no Gmail draft to send")
    if state == "already_sent":
        return SendOutcome(
            True, sent_already=True, row=store.get(database_url, row_id),
        )
    if state == "race_lost":
        return SendOutcome(False, http_status=409, detail="a send for this row is already in progress")

    # state == "claimed": we own the send.
    if shadow:
        store.finalize_send(database_url, row_id, sent_message_id=None, shadow=True)
        logger.info("SHADOW send for row %s (draft %s) — not actually sent", row_id, draft_id)
        return SendOutcome(True, shadow=True, row=store.get(database_url, row_id))

    from app.ingestion import gmail_write

    try:
        result = gmail_write.send_draft(
            account=row["account"], draft_id=draft_id, dry_run=dry_run, backend=backend,
        )
    except NotImplementedError as exc:
        store.abort_send(database_url, row_id)
        return SendOutcome(False, http_status=501, detail=str(exc))
    except gmail_write.GmailWriteError as exc:
        store.abort_send(database_url, row_id)
        return SendOutcome(False, http_status=502, detail=f"Gmail send failed: {exc}")
    except Exception as exc:  # noqa: BLE001 — never leave a row stuck in 'sending'
        store.abort_send(database_url, row_id)
        logger.warning("send_pending_row: unexpected error for row %s: %s", row_id, exc)
        return SendOutcome(False, http_status=500, detail=f"send failed: {exc}")

    if dry_run:
        # gog ran but didn't send — roll the claim back so it can really send later.
        store.abort_send(database_url, row_id)
        return SendOutcome(True, shadow=True, row=store.get(database_url, row_id), detail="dry-run (gog --dry-run): not sent")

    store.finalize_send(database_url, row_id, sent_message_id=result.message_id)
    return SendOutcome(
        True, sent_message_id=result.message_id, row=store.get(database_url, row_id),
    )
