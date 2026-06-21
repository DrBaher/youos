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
# non-string / wrong-length base64 bodies, and pathologically deep (or cyclic)
# ``parts`` nesting. They degrade (return empty/best-effort) rather than raise —
# a single crafted message must never abort the sweep. ``_MAX_MIME_DEPTH`` bounds
# the recursion well below CPython's limit; real mail nests only a few levels.
_MAX_MIME_DEPTH = 30

# Inbound bodies are attacker-influenced and length-unbounded; the stored body
# is later scored/classified in full (O(size) per message), so cap it at fetch
# time. 50 KB is far more than any reply needs as context. The cap is enforced on
# the base64 INPUT (see ``_decode_b64``) so a multi-MB body is never fully
# decoded into memory, which also bounds the text/html tag-stripping regex below.
_MAX_BODY_CHARS = 50_000

# The ``From``/``Subject`` headers are attacker-influenced too and reach the same
# stored/logged/LLM-prompt consumers as the body, so bound them the same way. A
# real header never legitimately needs more than a few hundred chars.
_MAX_HEADER_CHARS = 4096


def _decode_mime_words(value: str) -> str:
    """Decode RFC 2047 encoded-words in a header value (``=?utf-8?B?...?=``) to a
    readable Unicode string. Bounded to ``_MAX_HEADER_CHARS`` (the header is
    attacker-controlled). Returns the input unchanged when there is nothing to
    decode or on any decode error; never raises (degrade, don't crash)."""
    if not value:
        return ""
    value = value[:_MAX_HEADER_CHARS]
    if "=?" not in value:
        return value
    from email.header import decode_header
    try:
        parts = decode_header(value)
    except (ValueError, UnicodeDecodeError):
        return value
    out: list[str] = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(enc or "utf-8", errors="replace"))
            except (LookupError, TypeError):  # unknown/invalid charset label
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


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


def _charset_of(part: Any) -> str:
    """Best-effort charset label from a part's ``Content-Type`` header
    (e.g. ``text/plain; charset="ISO-8859-1"``). Defaults to UTF-8. Tolerates a
    malformed header list; never raises."""
    if not isinstance(part, dict):
        return "utf-8"
    for h in part.get("headers", []) or []:
        if isinstance(h, dict) and (h.get("name") or "").lower() == "content-type":
            m = re.search(r'charset="?([\w.:+-]+)"?', h.get("value", "") or "", re.IGNORECASE)
            if m:
                return m.group(1)
    return "utf-8"


def _decode_b64(data: Any, charset: str = "utf-8", max_chars: int | None = None) -> str:
    """Decode a Gmail urlsafe-base64 body to text, degrading to '' on any
    malformed input (non-string, wrong length). Decodes with ``charset`` (from
    the part's ``Content-Type``) and falls back to UTF-8 for an unknown/invalid
    charset label; undecodable bytes are replaced rather than raised.

    When ``max_chars`` is given, the base64 *input* is capped first so a
    multi-MB attacker body is never fully materialised in memory, and the
    decoded text is then sliced to ``max_chars``."""
    if not isinstance(data, str):
        return ""
    if max_chars is not None:
        # base64 expands 3 bytes → 4 chars and one UTF-8 char is at most 4 bytes,
        # so 6*max_chars input chars always decode to ≥ max_chars characters.
        # Rounded down to a whole 4-char base64 group so truncation stays valid.
        keep = (6 * max_chars) & ~0b11
        data = data[:keep]
    try:
        raw = base64.urlsafe_b64decode(data + "===")
    except (ValueError, TypeError):  # binascii.Error (a ValueError) / bad input
        return ""
    try:
        text = raw.decode(charset, errors="replace")
    except (LookupError, TypeError):  # unknown/invalid charset label
        text = raw.decode("utf-8", errors="replace")
    return text if max_chars is None else text[:max_chars]


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
            return _decode_b64(data, _charset_of(p), _MAX_BODY_CHARS)
        if mime == "text/html" and data:
            html = _decode_b64(data, _charset_of(p), _MAX_BODY_CHARS)
            return re.sub(r"<[^>]+>", " ", html)
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
    include_read: bool = False,
    include_read_window: str | None = None,
    own_emails: set[str] | None = None,
) -> list[InboxMessage]:
    """Fetch inbox threads and return the latest message in each.

    By default fetches only *unread* mail within ``window`` (a Gmail
    ``newer_than:`` token like ``"3d"``/``"7d"``). With ``include_read=True`` it
    fetches read+unread inbox mail and skips any thread whose latest message is
    your own (``own_emails``): if you sent the last message you've already
    answered it, so it's not awaiting a reply from you. ``include_read_window``
    bounds that scan to a (generous) ``newer_than:`` ceiling so a large/old inbox
    can't make every sweep fetch hundreds of threads; ``None`` = no ceiling.
    ``limit`` caps threads pulled; ``backend`` overrides the configured backend.

    Returns the *latest* message per thread — the one you'd naturally reply to.
    """
    from app.ingestion.adapters import get_google_source

    source = get_google_source(backend)
    # include_read: drop ``is:unread`` so the sweep sees still-in-inbox read mail,
    # bounded by an optional age ceiling (``include_read_window``) + ``limit`` +
    # the daily draft cap downstream.
    if include_read:
        query = "in:inbox" + (f" newer_than:{include_read_window}" if include_read_window else "")
    else:
        query = f"in:inbox is:unread newer_than:{window}"
    threads = source.search_threads(account=account, query=query, max_threads=limit) or []
    _own = {e.lower() for e in (own_emails or set()) if e}

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
            msg_obj = _inbox_message_from_thread(thread, account=account, thread_id=thread_id)
            # Unanswered filter (include_read only): if YOU sent the latest message
            # the thread is answered — you're awaiting THEM, not the reverse — so
            # skip it. An unread thread is never your own sent mail, so this is a
            # no-op in the default path.
            if include_read and _own and (msg_obj.sender_email or "").lower() in _own:
                continue
            results.append(msg_obj)
        except Exception:
            # A fetch OR parse failure on one thread shouldn't kill the whole
            # sweep — skip it and keep going.
            logger.warning("inbox fetch: skipping thread %s (fetch/parse failed)", thread_id, exc_info=True)
            continue
    return results


def _inbox_message_from_thread(thread: dict, *, account: str, thread_id: str) -> InboxMessage:
    """Parse a fetched thread payload into the latest-message ``InboxMessage``
    (the one you'd reply to) + up to 4 prior turns as context. Shared by the
    sweep (``fetch_unread``) and the on-demand single-thread fetch."""
    from app.core.sender import extract_email

    messages = thread.get("messages", []) or [thread]
    msg = messages[-1]
    payload = msg.get("payload", {}) or {}
    sender = _decode_mime_words(_header(payload, "From"))
    thread_history: list[dict[str, str]] = []
    # Capture up to 6 prior turns at 500 chars each (was 4 × 200) so the drafter
    # sees more of the conversation — enough to catch personal remarks, prior
    # commitments, and what an earlier "same time" refers to. Bounded by the
    # overall inbound cap (generation.max_inbound_chars) downstream.
    for prev in messages[:-1][-6:]:
        prev_payload = prev.get("payload", {}) or {}
        prev_text = _extract_text(prev_payload)
        if not prev_text:
            continue
        thread_history.append({
            "sender": _decode_mime_words(_header(prev_payload, "From") or "")[:80],
            "text": prev_text[:500],
        })
    return InboxMessage(
        message_id=msg.get("id") or msg.get("messageId") or thread_id,
        thread_id=thread_id,
        account=account,
        sender=sender,
        sender_email=extract_email(sender),
        subject=_decode_mime_words(_header(payload, "Subject")) or "(no subject)",
        body=_extract_text(payload),
        headers=_all_headers(payload),
        received_at=_header(payload, "Date") or None,
        has_attachment=_has_attachment(payload),
        thread_history=thread_history,
    )


def fetch_thread(account: str, thread_id: str, *, backend: str | None = None) -> InboxMessage | None:
    """Fetch ONE thread by id and return its latest-message ``InboxMessage`` (or
    None if it can't be fetched/parsed). Used for on-demand drafting of a thread
    the sweep never queued (read / hard-skipped / brand-new)."""
    from app.ingestion.adapters import get_google_source

    if not thread_id:
        return None
    try:
        thread = get_google_source(backend).get_thread(account=account, thread_id=thread_id)
        return _inbox_message_from_thread(thread, account=account, thread_id=thread_id)
    except Exception:
        logger.warning("fetch_thread: could not fetch/parse thread %s", thread_id, exc_info=True)
        return None
