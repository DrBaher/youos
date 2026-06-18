"""Gmail push-notification webhook (b282).

Real-time triage: instead of polling every ~15 min, register a Gmail
``users.watch`` against a Cloud Pub/Sub topic; Gmail publishes a tiny
``{emailAddress, historyId}`` notification whenever the mailbox changes, Pub/Sub
pushes it here, and YouOS fires an immediate triage sweep — so a new email is
drafted within seconds.

This endpoint is **public** (Pub/Sub can't send a PIN or a session cookie), so:

* it is **inert unless configured** — ``agent.gmail_push.enabled`` AND a
  ``agent.gmail_push.token`` must both be set;
* it self-authenticates with that shared secret in the URL (``?token=…``),
  compared in constant time. Pub/Sub appends the query string you register the
  push subscription with;
* it acks fast (Pub/Sub treats any non-2xx as failure and retries with backoff),
  running the sweep in the background, debounced by the existing
  ``triage_min_interval_seconds`` so a burst of notifications coalesces to one
  sweep.

The watch registration + Pub/Sub topic live in your Google Cloud project — see
``integrations/gmail-pubsub/README.md``. This handler is the YouOS side.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from secrets import compare_digest

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

router = APIRouter()
logger = logging.getLogger(__name__)

# Path the auth middleware must exempt from PIN/cookie/token auth (this endpoint
# does its OWN shared-secret check). Kept here so main.py imports one source.
PUSH_PATH = "/api/gmail/push"


def _push_config() -> dict[str, object]:
    from app.core.config import load_config

    cfg = load_config() or {}
    agent = (cfg.get("agent") or {}) if isinstance(cfg, dict) else {}
    gp = (agent.get("gmail_push") or {}) if isinstance(agent, dict) else {}
    if not isinstance(gp, dict):
        gp = {}
    return {"enabled": bool(gp.get("enabled", False)), "token": str(gp.get("token") or "")}


def _decode_email(body: dict) -> str | None:
    """The notified mailbox from a Pub/Sub push envelope, or None if malformed.

    Envelope: ``{"message": {"data": base64(json({"emailAddress","historyId"}))}}``.
    """
    message = (body or {}).get("message") or {}
    data = message.get("data")
    if not data:
        return None
    try:
        payload = json.loads(base64.b64decode(data).decode("utf-8"))
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError):
        return None
    email = payload.get("emailAddress") if isinstance(payload, dict) else None
    return str(email) if email else None


def _match_account(email: str) -> str | None:
    """Map the notified mailbox to a configured YouOS account (case-insensitive),
    or None if it isn't one we triage — so a stray notification is a no-op."""
    if not email:
        return None
    target = email.strip().lower()
    try:
        from app.core.config import get_ingestion_accounts

        for acct in get_ingestion_accounts():
            if str(acct).strip().lower() == target:
                return str(acct)
    except Exception:
        pass
    return None


def _sweep_account(account: str) -> None:
    """Run a triage sweep for ``account``, debounced: skip if one ran within the
    configured ``triage_min_interval_seconds`` (a burst of pushes coalesces to a
    single sweep). run_triage holds a per-account lock, so this never piles up."""
    from datetime import datetime, timezone

    from app.agent import store
    from app.agent.scheduler import get_agent_config
    from app.agent.triage import run_triage
    from app.core.settings import get_settings

    settings = get_settings()
    min_interval = int(get_agent_config().get("triage_min_interval_seconds", 60))
    if min_interval > 0:
        try:
            recent = store.list_recent_sweeps(settings.database_url, account=account, limit=1)
            last_ts = (recent[0].get("started_at") if recent else None) or None
            if last_ts:
                last = datetime.fromisoformat(last_ts)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - last).total_seconds() < min_interval:
                    logger.info("gmail_push: skipping sweep for %s (debounced)", account)
                    return
        except Exception:
            logger.debug("gmail_push: debounce check failed; sweeping anyway", exc_info=True)
    try:
        run_triage(account=account, trigger="gmail_push")
    except Exception:
        logger.warning("gmail_push: triage sweep failed for %s", account, exc_info=True)


@router.post(PUSH_PATH)
async def gmail_push(request: Request, background: BackgroundTasks) -> dict:
    cfg = _push_config()
    if not cfg["enabled"] or not cfg["token"]:
        # Inert until configured — don't advertise the endpoint otherwise.
        raise HTTPException(404, "gmail push not enabled")
    if not compare_digest(request.query_params.get("token", ""), str(cfg["token"])):
        raise HTTPException(403, "invalid push token")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid push body") from None

    email = _decode_email(body)
    if not email:
        # Malformed/empty — ack so Pub/Sub stops retrying a hopeless message.
        return {"ok": True, "ignored": "no emailAddress in notification"}
    account = _match_account(email)
    if not account:
        return {"ok": True, "ignored": f"{email} is not a configured account"}

    background.add_task(_sweep_account, account)
    return {"ok": True, "account": account, "sweep": "scheduled"}
