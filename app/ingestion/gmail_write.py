"""Gmail-write capability for Phase 2 of the agent loop.

Currently exposes ``create_draft(...)`` — pushes an agent-generated draft
into the user's Gmail Drafts folder as a real reply on the original thread
(so the user finishes-and-sends from Gmail). **Never sends.** The agent
loop's "Mark sent" path remains a separate signal for "I sent it manually
elsewhere."

Backend dispatch follows the existing ``ingestion.google_backend`` setting
(``gog`` / ``gws`` / ``native``). ``gog`` is implemented because the user's
authenticated path runs through it; ``gws`` + ``native`` raise a clear
``NotImplementedError`` until Phase 2.2 adds them.

If you need to fix the exact ``gog gmail drafts create`` invocation, the
command lives in ``_gog_create_draft`` and is the only thing that should
change — the abstraction + the caller are stable.
"""

from __future__ import annotations

import base64
import email.message
import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class GmailWriteError(RuntimeError):
    """Raised when a backend can't create the draft. Caller (the agent
    route) translates to an HTTP error with a useful message."""


@dataclass
class GmailDraftResult:
    """Outcome of a successful draft creation. ``draft_id`` is Gmail's id
    for the new draft (the value we persist on the row); ``raw_response``
    is the backend's full payload, kept for debugging."""

    draft_id: str
    raw_response: dict[str, Any]


def create_draft(
    *,
    account: str,
    thread_id: str | None,
    to_email: str,
    subject: str,
    body: str,
    backend: str | None = None,
) -> GmailDraftResult:
    """Create a Gmail draft on ``thread_id`` (if set) addressed to ``to_email``.

    ``account`` is the Google account that *owns* the draft (the user's
    address, not the recipient's). ``backend`` overrides the configured
    ``ingestion.google_backend``; leave None to use the default.
    """
    from app.core.config import get_ingestion_google_backend

    name = (backend or get_ingestion_google_backend()).strip().lower()
    if name == "gog":
        return _gog_create_draft(
            account=account, thread_id=thread_id,
            to_email=to_email, subject=subject, body=body,
        )
    if name == "gws":
        raise NotImplementedError(
            "gws backend doesn't yet support draft creation — "
            "implementation in Phase 2.2. Use the gog backend for now."
        )
    if name == "native":
        raise NotImplementedError(
            "native backend draft creation needs the gmail.compose OAuth "
            "scope; one-time re-auth is required. Implementation in Phase 2.2."
        )
    raise ValueError(f"unknown ingestion.google_backend: {name!r}")


# --- gog backend -----------------------------------------------------------


def _build_rfc822(
    *, to_email: str, subject: str, body: str,
) -> bytes:
    """Build an RFC 822 message. Subject already includes the ``Re:`` prefix
    if the caller wants threading; Gmail handles thread continuity from the
    explicit ``thread_id`` we pass alongside the message."""
    msg = email.message.EmailMessage()
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    return msg.as_bytes()


def _gog_create_draft(
    *,
    account: str,
    thread_id: str | None,
    to_email: str,
    subject: str,
    body: str,
) -> GmailDraftResult:
    """Best-effort ``gog gmail drafts create`` invocation.

    Builds an RFC 822 message, base64url-encodes it, and passes via
    ``--raw`` — the wire-level format Gmail's drafts.create API expects.
    Thread continuity comes from ``--thread-id``.

    NOTE: the exact gog subcommand and flag names here are based on the
    Google API shape; if gog uses different names (e.g. ``drafts.create``
    instead of ``drafts create``), this is the single function to fix.
    See ``gog gmail drafts --help`` to verify on your local install.
    """
    rfc = _build_rfc822(to_email=to_email, subject=subject, body=body)
    raw_b64 = base64.urlsafe_b64encode(rfc).decode("ascii")

    cmd: list[str] = [
        "gog", "gmail", "drafts", "create",
        "--account", account,
        "--json", "--no-input",
        "--raw", raw_b64,
    ]
    if thread_id:
        cmd += ["--thread-id", thread_id]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise GmailWriteError("gog CLI not on PATH — install via Homebrew or set up the native backend") from exc
    except subprocess.TimeoutExpired as exc:
        raise GmailWriteError("gog gmail drafts create timed out (30s)") from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise GmailWriteError(f"gog returned exit {result.returncode}: {stderr or 'no stderr'}")

    # The Google API returns a Draft resource: { "id": "...", "message": {...} }.
    # gog passes this through unchanged when --json is set.
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise GmailWriteError(f"gog returned non-JSON stdout: {result.stdout[:200]!r}") from exc

    draft_id = payload.get("id") or payload.get("draftId") or ""
    if not draft_id:
        raise GmailWriteError(f"gog returned no draft id; payload={payload!r}")

    logger.info("created gmail draft %s for account=%s thread=%s", draft_id, account, thread_id)
    return GmailDraftResult(draft_id=str(draft_id), raw_response=payload)
