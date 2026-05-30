"""Sender classification for sender-aware retrieval."""

from __future__ import annotations

import re
from typing import Literal

from app.core.config import get_internal_domains

SenderType = Literal["internal", "external_client", "personal", "automated", "unknown"]

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")

# A ``From`` header is attacker-controlled and length-unbounded. ``_EMAIL_RE``
# backtracks O(n^2) on a long run of non-``@`` characters (a 100 KB no-``@``
# header hangs ~30 s), so we bound the window the regex is ever handed.
_MAX_ADDR_SCAN = 1024

_TITLE_PREFIXES = re.compile(r"^(dr\.?|prof\.?|mr\.?|mrs\.?|ms\.?|sir)\s+", re.IGNORECASE)


def first_name_from_display_name(display_name: str | None) -> str | None:
    """Extract first name from a display name string.

    Handles: "Sarah Mitchell", "Dr. Baher", "sarah.mitchell@company.com", etc.
    Returns None if unparseable.
    """
    if not display_name or not display_name.strip():
        return None

    name = display_name.strip()

    # If it looks like an email, extract from local part
    if "@" in name:
        local = name.split("@")[0]
        # Split on dots, hyphens, underscores
        parts = re.split(r"[._\-]", local)
        if parts and parts[0]:
            return parts[0].capitalize()
        return None

    # Strip titles
    name = _TITLE_PREFIXES.sub("", name).strip()

    if not name:
        return None

    # Take first word as first name
    first = name.split()[0]
    # Remove any trailing punctuation
    first = first.rstrip(",.")
    if not first:
        return None
    return first[0].upper() + first[1:] if len(first) > 1 else first.upper()


_AUTOMATED_PREFIXES = frozenset(
    {
        "no-reply",
        "noreply",
        "donotreply",
        "do-not-reply",
        "invoice",
        "billing",
        "mailer",
        "notifications",
        "support",
        "bounce",
        "postmaster",
        "daemon",
    }
)

_PERSONAL_DOMAINS = frozenset(
    {
        "gmail.com",
        "yahoo.com",
        "hotmail.com",
        "icloud.com",
        "me.com",
        "outlook.com",
        "live.com",
        "aol.com",
        "protonmail.com",
        "proton.me",
        "fastmail.com",
    }
)


def _find_email(author: str | None) -> str | None:
    """Return the bare ``local@domain`` address from an author/``From`` field,
    or ``None``. Hardened against two attacker-controlled hazards:

    * **ReDoS** — a long no-``@`` header makes ``_EMAIL_RE`` backtrack O(n^2).
      We pull the address from inside angle brackets (linear ``rfind``/``find``,
      no regex) and cap the scan window, so the regex only ever sees a bounded
      string that already contains an ``@``.
    * **Multi-``@`` spoofing** — ``Name <a@b@c.com>`` would otherwise yield the
      wrong address (``b@c.com``) and mis-route skip/VIP/whitelist/domain rules,
      and ``evil@spoof.com <real@host.com>`` would return the display-name
      address. We take the addr-spec verbatim from inside angle brackets and
      reject an ambiguous multi-``@`` single token rather than guess.
    """
    if not author:
        return None
    # Prefer the addr-spec inside angle brackets (RFC 5322 "Display Name <addr>").
    # rfind/find are linear and run before any regex, so a huge display name (or
    # a huge bracket-less header) can't blow up the scan.
    lt = author.rfind("<")
    if lt != -1:
        gt = author.find(">", lt + 1)
        candidate = (author[lt + 1 : gt] if gt != -1 else author[lt + 1 :]).strip()
    else:
        candidate = author.strip()
    candidate = candidate[:_MAX_ADDR_SCAN]
    if "@" not in candidate:
        return None
    if candidate.count("@") == 1:
        match = _EMAIL_RE.search(candidate)
        return _reject_dash_leading(match.group()) if match else None
    # More than one "@": either a malformed single addr-spec (``a@b@c.com`` →
    # reject, never mis-extract) or an address list (``a@x.com, b@y.com`` → take
    # the first valid single-``@`` token).
    for token in re.split(r"[\s,;]+", candidate):
        if token.count("@") != 1:
            continue
        match = _EMAIL_RE.search(token)
        if match:
            return _reject_dash_leading(match.group())
    return None


def _reject_dash_leading(email: str | None) -> str | None:
    """Drop an addr-spec whose local part starts with ``-``. It isn't a real
    address and, passed as gog's ``--to`` value, the Kong arg parser reads it as
    a flag (exit 2) — fail closed rather than emit a poisoned recipient."""
    if email and email.startswith("-"):
        return None
    return email


def extract_domain(author: str | None) -> str | None:
    """Extract the domain from an email address in the author string."""
    email = _find_email(author)
    if not email:
        return None
    return email.split("@", 1)[1].lower()


def extract_email(author: str | None) -> str | None:
    """Extract the full ``local@domain`` email address from an ``author``
    field that may be ``"Name <email@host>"`` or just an email. Lowercased.
    Returns ``None`` if no email is found."""
    email = _find_email(author)
    return email.lower() if email else None


def classify_sender(author: str | None) -> SenderType:
    """Classify a sender into a category based on their email address."""
    email = _find_email(author)
    if not email:
        return "unknown"

    email = email.lower()
    local, domain = email.split("@", 1)

    # Check automated first (overrides domain checks)
    local_base = local.replace(".", "").replace("-", "").replace("_", "")
    for prefix in _AUTOMATED_PREFIXES:
        normalized = prefix.replace("-", "")
        if local_base == normalized or local_base.startswith(normalized):
            return "automated"

    # Check internal domains from user config
    internal_domains = get_internal_domains()
    if domain in internal_domains:
        return "internal"

    if domain in _PERSONAL_DOMAINS:
        return "personal"

    return "external_client"
