"""Sender classification for sender-aware retrieval."""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Literal

from app.core.config import get_internal_domains

logger = logging.getLogger(__name__)

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


# Hard automated signals: a local part that begins with any of these is a
# machine/bulk sender with near-certainty. Matched on the dash/dot/underscore-
# stripped local part, so ``no-reply``/``no_reply``/``noreply`` all normalize to
# the same ``noreply`` token (see ``_local_is_hard_automated``).
#
# b190 broadened this set: the nightly's coarse buckets came partly from
# automated mail slipping through into ``external_client`` because the prefix
# list missed common bulk-mail locals (``mailer-daemon``, ``bounces``,
# ``newsletter``, ``marketing``, ``alerts``, ``updates``…).
_AUTOMATED_PREFIXES = frozenset(
    {
        "no-reply",
        "noreply",
        "donotreply",
        "do-not-reply",
        "invoice",
        "billing",
        "mailer",
        "mailer-daemon",
        "notification",
        "notifications",
        "notify",
        "bounce",
        "bounces",
        "postmaster",
        "daemon",
        "automated",
        "automailer",
        "marketing",
        "newsletter",
        "newsletters",
        "alert",
        "alerts",
        "updates",
        "noreplies",
    }
)

# Soft automated signals: role mailboxes. ``info@``/``support@``/``sales@`` are
# *often* automated, but a real human at a small company genuinely answers from
# ``info@``. b190 keeps these out of the hard set so we don't mislabel a person
# as ``automated`` (which would route them to the wrong persona adapter). They
# only tip a sender to ``automated`` when paired with another machine signal —
# currently used as a documented soft hint, never a hard override.
_ROLE_MAILBOX_PREFIXES = frozenset(
    {
        "info",
        "support",
        "sales",
        "hello",
        "contact",
        "admin",
        "team",
        "office",
        "help",
        "service",
        "services",
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


def _normalize_local(local: str) -> str:
    """Collapse a local part to its alphanumeric core so ``no-reply``,
    ``no_reply`` and ``noreply`` all compare equal."""
    return local.replace(".", "").replace("-", "").replace("_", "")


def _local_is_hard_automated(local: str) -> bool:
    """True when the local part begins with a hard automated prefix
    (``noreply``, ``mailer-daemon``, ``bounce``, ``newsletter``…)."""
    local_base = _normalize_local(local)
    for prefix in _AUTOMATED_PREFIXES:
        normalized = prefix.replace("-", "").replace("_", "")
        if local_base == normalized or local_base.startswith(normalized):
            return True
    return False


def _local_is_role_mailbox(local: str) -> bool:
    """True when the local part is a shared role mailbox (``info``,
    ``support``, ``sales``…). A *soft* signal — never a hard ``automated``
    override, since a real person can answer from one at a small company."""
    local_base = _normalize_local(local)
    return any(local_base == role for role in _ROLE_MAILBOX_PREFIXES)


# Profile-stored sender_type values we trust enough to short-circuit the
# heuristics. ``unknown`` is excluded so a stale/under-determined profile can't
# pin a live sender to ``unknown``; we re-derive instead.
_PROFILE_TRUSTED_TYPES = frozenset({"internal", "external_client", "personal", "automated"})


def _classify_from_address(email: str) -> SenderType:
    """Heuristic classification from a bare lowercased ``local@domain``.

    Order: hard-automated local part → internal domain (user config) →
    personal free-mail domain → ``external_client`` fall-through. A
    successfully-extracted address is *never* ``unknown`` — ``unknown`` is
    reserved for "no parseable sender" (handled by the caller)."""
    local, domain = email.split("@", 1)

    # Hard automated signals override everything (a noreply@ at an internal
    # domain is still machine mail, not a colleague).
    if _local_is_hard_automated(local):
        return "automated"

    # User-configured internal domains.
    if domain in get_internal_domains():
        return "internal"

    # Free-mail providers → personal. A role mailbox can't exist on these in
    # practice, so the role check below never fires for them.
    if domain in _PERSONAL_DOMAINS:
        return "personal"

    # Everything else with a parseable address is an external correspondent.
    # Role mailboxes (info@/support@) stay here as humans-by-default; they are
    # a soft hint surfaced via ``classify_sender_detail``, not a hard flip to
    # ``automated``.
    return "external_client"


def classify_sender(author: str | None, database_url: str | None = None) -> SenderType:
    """Classify a sender into a category based on their email address.

    When ``database_url`` is supplied, a matching ``sender_profiles`` row (by
    exact email, then by domain) takes precedence: its stored ``sender_type``
    is reused for cross-session consistency and richer-than-heuristic accuracy
    (the profile was built from real reply history). Without a profile or a
    DB, falls back to the deterministic heuristics in ``_classify_from_address``.

    Backward-compatible: the historical single-arg form is unchanged — callers
    that don't pass ``database_url`` get the pure heuristic path.
    """
    return classify_sender_detail(author, database_url).sender_type


@dataclass(frozen=True)
class SenderClassification:
    """Result of :func:`classify_sender_detail`.

    ``source`` is one of ``profile_email`` / ``profile_domain`` / ``heuristic``
    / ``none`` and ``reason`` is a short human-readable explanation, handy for
    debugging routing decisions and for the ``/sender`` UI. ``company`` /
    ``relationship_note`` are populated when an enriching profile was found."""

    sender_type: SenderType
    source: str
    reason: str
    company: str | None = None
    relationship_note: str | None = None


def _lookup_profile_type(email: str, domain: str | None, database_url: str) -> SenderClassification | None:
    """Read an enriched classification from ``sender_profiles`` for ``email``
    (exact, preferred) or ``domain`` (fallback). Returns ``None`` when the
    table/row is absent or the stored type isn't trustworthy. Never raises —
    a profile read must not break classification."""
    try:
        from app.db.bootstrap import resolve_sqlite_path  # local import: avoid import cycle / optional dep

        db_path = resolve_sqlite_path(database_url)
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            con.row_factory = sqlite3.Row
            exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sender_profiles'"
            ).fetchone()
            if not exists:
                return None
            row = con.execute(
                "SELECT sender_type, company, relationship_note FROM sender_profiles WHERE email = ? LIMIT 1",
                (email,),
            ).fetchone()
            source = "profile_email"
            if row is None and domain:
                row = con.execute(
                    "SELECT sender_type, company, relationship_note FROM sender_profiles "
                    "WHERE domain = ? AND sender_type IS NOT NULL AND sender_type <> '' "
                    "ORDER BY reply_count DESC LIMIT 1",
                    (domain,),
                ).fetchone()
                source = "profile_domain"
            if row is None:
                return None
            stored = (row["sender_type"] or "").strip().lower()
            if stored not in _PROFILE_TRUSTED_TYPES:
                return None
            company = row["company"] if "company" in row.keys() else None
            note = row["relationship_note"] if "relationship_note" in row.keys() else None
            return SenderClassification(
                sender_type=stored,  # type: ignore[arg-type]
                source=source,
                reason=f"sender_profiles {source.split('_')[1]} match → {stored}",
                company=company,
                relationship_note=note,
            )
        finally:
            con.close()
    except Exception as exc:  # defensive: classification must not break on DB errors
        # Still fail safe (heuristics take over), but don't swallow silently —
        # a persistent profile-lookup failure (locked DB, schema drift, perms)
        # would otherwise be invisible while every sender quietly de-enriches.
        logger.warning("sender profile lookup failed, falling back to heuristics: %s", exc)
        return None


def classify_sender_detail(author: str | None, database_url: str | None = None) -> SenderClassification:
    """Like :func:`classify_sender` but returns the type *plus* provenance
    (``source``/``reason``) and any enriching ``company``/``relationship_note``.

    ``unknown`` is returned **only** when no email can be parsed from
    ``author``. Any successfully-extracted address resolves to a concrete type
    (profile lookup first, then heuristics defaulting to ``external_client``)."""
    email = _find_email(author)
    if not email:
        return SenderClassification("unknown", source="none", reason="no parseable sender address")

    email = email.lower()
    domain = email.split("@", 1)[1]

    # 1) Enriched profile lookup (cross-session consistency, real history).
    if database_url:
        profile = _lookup_profile_type(email, domain, database_url)
        if profile is not None:
            return profile

    # 2) Deterministic heuristics — never returns ``unknown`` for a parsed addr.
    sender_type = _classify_from_address(email)
    local = email.split("@", 1)[0]
    if sender_type == "external_client" and _local_is_role_mailbox(local):
        reason = "role mailbox (soft hint); classified external_client (human by default)"
    else:
        reason = f"heuristic → {sender_type}"
    return SenderClassification(sender_type, source="heuristic", reason=reason)
