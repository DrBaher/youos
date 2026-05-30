"""Fetch unread inbox threads for triage.

Reuses the configured ingestion backend (``ingestion.google_backend``:
``gog``/``gws``/``native``) so authentication, rate limiting, and account
selection match what ``youos ingest`` already does. The only difference is
the Gmail search query: ``in:inbox is:unread`` instead of ``in:sent``.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class InboxMessage:
    """One unread inbox message normalised across the three backends.

    ``thread_id`` lets us dedupe and link back to the original conversation;
    ``message_id`` identifies the specific unread message inside it.
    """

    message_id: str
    thread_id: str
    account: str
    sender: str                       # full ``"Name <email>"`` as stored
    sender_email: str | None          # parsed local@domain (lowercased)
    subject: str
    body: str                         # text/plain (falls back to stripped text/html)
    headers: dict[str, str] = field(default_factory=dict)
    received_at: str | None = None    # ISO/RFC822 Date header if available
    has_attachment: bool = False      # any payload part carries a filename
    # Prior turns in the same thread (oldest→newest, excluding the latest
    # message which is ``body``). Each entry is ``{"sender": ..., "text": ...}``
    # — the shape generation's ``_format_thread_context`` consumes. Lets the
    # drafter see the conversation so it doesn't answer the wrong question in a
    # multi-turn thread. Empty for a brand-new inbound.
    thread_history: list[dict[str, str]] = field(default_factory=list)


# Email payloads are attacker-influenced (anyone can send the user mail), so the
# parse helpers below defend against malformed MIME: non-dict headers/parts,
# non-string / wrong-length base64 bodies, and pathologically deep ``parts``
# nesting. They degrade (return empty/best-effort) rather than raise — a single
# crafted message must never abort the sweep. ``_MAX_MIME_DEPTH`` bounds the
# recursion well below CPython's limit; real mail nests only a few levels.
_MAX_MIME_DEPTH = 30


def _header(payload: dict[str, Any], name: str) -> str:
    """Look up a Gmail header value (case-insensitive). Tolerates a malformed
    header list (non-list, or non-dict entries)."""
    if not isinstance(payload, dict):
        return ""
    for h in payload.get("headers", []) or []:
        if isinstance(h, dict) and str(h.get("name", "")).lower() == name.lower():
            return h.get("value", "") or ""
    return ""


def _all_headers(payload: dict[str, Any]) -> dict[str, str]:
    """Flatten the payload's header list into a lowercased dict. Tolerates a
    malformed header list (non-list, or non-dict entries)."""
    out: dict[str, str] = {}
    if not isinstance(payload, dict):
        return out
    for h in payload.get("headers", []) or []:
        if not isinstance(h, dict):
            continue
        name = (h.get("name") or "").lower()
        if name and name not in out:
            out[name] = h.get("value", "") or ""
    return out


def _decode_b64(data: Any) -> str:
    """Decode a Gmail urlsafe-base64 body to text, degrading to '' on any
    malformed input (non-string, wrong length, non-ASCII)."""
    if not isinstance(data, str):
        return ""
    try:
        return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
    except (ValueError, TypeError):  # binascii.Error (a ValueError) / bad input
        return ""


def _extract_text(payload: dict[str, Any]) -> str:
    """Pull ``text/plain`` (falling back to stripped ``text/html``) from a Gmail
    payload. Walks ``parts`` recursively (depth-bounded) until a body is found.
    Tolerates malformed parts; never raises."""
    def walk(p: Any, depth: int) -> str:
        if depth > _MAX_MIME_DEPTH or not isinstance(p, dict):
            return ""
        mime = p.get("mimeType", "")
        body = p.get("body", {}) or {}
        data = body.get("data") if isinstance(body, dict) else None
        if mime == "text/plain" and data:
            return _decode_b64(data)
        if mime == "text/html" and data:
            return re.sub(r"<[^>]+>", " ", _decode_b64(data))
        for part in p.get("parts", []) or []:
            r = walk(part, depth + 1)
            if r:
                return r
        return ""

    return walk(payload, 0).strip()


def _has_attachment(payload: dict[str, Any]) -> bool:
    """True if any MIME part carries a filename (i.e. a real attachment).
    Depth-bounded + malformed-part tolerant; never raises."""
    def walk(p: Any, depth: int) -> bool:
        if depth > _MAX_MIME_DEPTH or not isinstance(p, dict):
            return False
        if (p.get("filename") or "").strip():
            return True
        return any(walk(part, depth + 1) for part in p.get("parts", []) or [])
    return walk(payload, 0)


def message_age_days(received_at: str | None) -> float | None:
    """Age of a message in (fractional) days from its RFC822 ``Date`` header,
    or None if the header is missing/unparseable. Used by the ``older_than_days``
    / ``newer_than_days`` rule predicates."""
    if not received_at:
        return None
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(received_at)
    except (TypeError, ValueError, OverflowError, OSError):
        # OverflowError: an extreme year in the Date header overflows the
        # datetime constructor (matches the catch-set in gmail_threads /
        # google_docs). A bad date must yield None, never crash the sweep.
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    return max(0.0, delta.total_seconds() / 86400.0)


def fetch_unread(
    account: str,
    *,
    window: str = "7d",
    limit: int = 50,
    backend: str | None = None,
) -> list[InboxMessage]:
    """Fetch unread inbox threads and return the latest message in each.

    ``window`` is a Gmail ``newer_than:`` token (``"3d"``, ``"7d"``, ``"24h"``).
    ``limit`` caps the number of threads pulled. ``backend`` overrides the
    configured ``ingestion.google_backend`` (mainly for tests).

    Returns the *latest* message per thread — that's the one the user just
    received and would naturally reply to. Earlier messages in the same
    thread (already-read replies of yours, prior exchanges) are skipped.
    """
    from app.core.sender import extract_email
    from app.ingestion.adapters import get_google_source

    source = get_google_source(backend)
    query = f"in:inbox is:unread newer_than:{window}"
    threads = source.search_threads(account=account, query=query, max_threads=limit) or []

    results: list[InboxMessage] = []
    for t in threads[:limit]:
        thread_id = t.get("id") or t.get("threadId") or t.get("Id")
        if not thread_id:
            continue
        try:
            thread = source.get_thread(account=account, thread_id=thread_id)
            # Parsing runs on attacker-influenced MIME, so keep it INSIDE the
            # guard: a malformed message drops just that thread (continue), it
            # never aborts the sweep. The parse helpers also degrade internally;
            # this is the belt-and-suspenders boundary.
            messages = thread.get("messages", []) or [thread]
            msg = messages[-1]                                # latest = the unread one
            payload = msg.get("payload", {}) or {}
            sender = _header(payload, "From")
            # Prior turns (everything before the latest) so generation can draft
            # with conversation context. Keep the last 4 to bound the prompt;
            # truncate each body to ~200 chars (matches the regex-thread budget).
            thread_history: list[dict[str, str]] = []
            for prev in messages[:-1][-4:]:
                prev_payload = prev.get("payload", {}) or {}
                prev_text = _extract_text(prev_payload)
                if not prev_text:
                    continue
                thread_history.append({
                    "sender": (_header(prev_payload, "From") or "")[:80],
                    "text": prev_text[:200],
                })
            results.append(
                InboxMessage(
                    message_id=msg.get("id") or msg.get("messageId") or thread_id,
                    thread_id=thread_id,
                    account=account,
                    sender=sender,
                    sender_email=extract_email(sender),
                    subject=_header(payload, "Subject") or "(no subject)",
                    body=_extract_text(payload),
                    headers=_all_headers(payload),
                    received_at=_header(payload, "Date") or None,
                    has_attachment=_has_attachment(payload),
                    thread_history=thread_history,
                )
            )
        except Exception:
            # A fetch OR parse failure on one thread shouldn't kill the whole
            # sweep — skip it and keep going.
            logger.warning("inbox fetch: skipping thread %s (fetch/parse failed)", thread_id, exc_info=True)
            continue
    return results
