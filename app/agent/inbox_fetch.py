"""Fetch unread inbox threads for triage.

Reuses the configured ingestion backend (``ingestion.google_backend``:
``gog``/``gws``/``native``) so authentication, rate limiting, and account
selection match what ``youos ingest`` already does. The only difference is
the Gmail search query: ``in:inbox is:unread`` instead of ``in:sent``.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Any


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
    received_at: str | None = None    # ISO timestamp if available


def _header(payload: dict[str, Any], name: str) -> str:
    """Look up a Gmail header value (case-insensitive)."""
    for h in payload.get("headers", []) or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "") or ""
    return ""


def _all_headers(payload: dict[str, Any]) -> dict[str, str]:
    """Flatten the payload's header list into a lowercased dict."""
    out: dict[str, str] = {}
    for h in payload.get("headers", []) or []:
        name = (h.get("name") or "").lower()
        if name and name not in out:
            out[name] = h.get("value", "") or ""
    return out


def _extract_text(payload: dict[str, Any]) -> str:
    """Pull ``text/plain`` (falling back to stripped ``text/html``) from a Gmail
    payload. Walks ``parts`` recursively until a body is found."""
    def walk(p: dict[str, Any]) -> str:
        mime = p.get("mimeType", "")
        body = p.get("body", {}) or {}
        data = body.get("data")
        if mime == "text/plain" and data:
            return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
        if mime == "text/html" and data:
            html = base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
            return re.sub(r"<[^>]+>", " ", html)
        for part in p.get("parts", []) or []:
            r = walk(part)
            if r:
                return r
        return ""

    return walk(payload).strip()


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
        except Exception:
            # Individual thread fetch failures shouldn't kill the whole sweep.
            continue
        messages = thread.get("messages", []) or [thread]
        msg = messages[-1]                                # latest = the unread one
        payload = msg.get("payload", {}) or {}
        sender = _header(payload, "From")
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
            )
        )
    return results
