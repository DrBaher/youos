"""Detect that someone accepted one of the open slots the agent proposed.

When the agent drafts a reply to a meeting request it offers concrete open
times and persists them (``proposed_slots_json`` on the draft row, b265). When
the other party replies, this asks the warm local model one constrained
question: did they confirm exactly ONE of those specific times, and which?

It is deliberately conservative — it only fires on an unambiguous acceptance of
a slot WE proposed (so it can't invent a meeting), and it never creates
anything: a hit only queues a row in ``agent_pending_events`` for the user's
one-tap approval. Creation happens later, gated, in ``calendar_events``.

On-device (no egress); failure-isolated — model unavailable / unparseable
answer / no proposed slots ⇒ no detection, the sweep is unaffected.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_BODY_SNIPPET_CHARS = 600
# Token budget by tier: the on-device LoRA answers tersely; a cloud reasoning
# model needs room to think before committing on its FINAL: line.
_MAX_TOKENS_LOCAL = 16
_MAX_TOKENS_CLOUD = 256
# Self-scheduled extraction emits an ISO datetime on its FINAL line, so even the
# terse path needs room for it (and the cloud path room to resolve "Tuesday").
_MAX_TOKENS_SELF_LOCAL = 64
_MAX_TOKENS_SELF_CLOUD = 256
_ISO_DT_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\s*[+-]\d{2}:?\d{2})?")
_DEFAULT_MEETING_MINUTES = 30
# Cheap pre-filter for self-scheduled detection: a sent message worth asking the
# model about must contain a clock time OR scheduling vocabulary. Skips the
# model (cost + false positives) on the 90% of sent mail that isn't arranging a
# meeting. Requires an explicit time signal so "see you at the conference"
# (no clock time) doesn't even reach the model.
_MEETING_SIGNAL_RE = re.compile(
    r"\b\d{1,2}\s*(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b|"
    r"\b(?:meet|meeting|call|invite|calendar|schedul|appointment|catch[\s-]?up)\b",
    re.IGNORECASE,
)


@dataclass
class ConfirmationResult:
    """An accepted slot. ``start_iso``/``end_iso`` echo the chosen proposed slot
    verbatim (RFC3339 with offset); ``attendees`` is who to invite; ``title`` is
    the event summary; ``confidence`` in [0, 1]; ``reasons`` explain the call."""

    start_iso: str
    end_iso: str
    title: str
    attendees: list[str]
    confidence: float
    reasons: list[str]


def _coerce_index(token: str, n_slots: int) -> int | None:
    """A 1-based slot token → 0-based index in range, or None."""
    if not token or not token.isdigit():
        return None
    n = int(token)
    return n - 1 if 1 <= n <= n_slots else None


def _parse_choice(out: str, n_slots: int) -> int | None:
    """Map the model's answer to a 0-based slot index, or None (no acceptance).

    Two answer styles, both safe:

    * **Reasoning models** (e.g. Claude) think out loud, then commit on a
      ``FINAL: <n>|NONE`` line — the prompt asks for it. We read THAT line, so a
      model that reconsiders ("NONE — wait, 👍 is acceptance, so 1") lands on its
      conclusion, not its first cautious token.
    * **Terse models** (the on-device LoRA) answer with just the number/NONE — we
      anchor on the LEADING token.

    Either way we never mine a digit out of free prose (a stray number in an
    explanation must not become a wrong event)."""
    t = (out or "").strip()
    if not t:
        return None
    # Prefer an explicit FINAL: commitment anywhere in the output.
    fin = re.search(r"final\s*:\s*(none|\d+)", t, flags=re.IGNORECASE)
    if fin:
        tok = fin.group(1).lower()
        return None if tok == "none" else _coerce_index(tok, n_slots)
    low = t.lower()
    if low.startswith("none"):
        return None
    # A hedged/conditional answer is NOT a confirmation, even if it leads with a
    # number ("2, but only if my flight lands", "1 tentatively", "3?"). The terse
    # on-device model can emit these without a FINAL line, and the leading-number
    # anchor below would otherwise mine the digit into a wrong event.
    if re.search(r"\b(but|if|unless|maybe|perhaps|might|tentativ|depend|possibl|provid)", low) or "?" in low:
        return None
    # Terse path: a leading number (optionally after slot/option/number/#).
    m = re.match(r"(?:slot|option|number|no\.?)?\s*#?\s*(\d+)\b", low)
    return _coerce_index(m.group(1), n_slots) if m else None


def _strip_re(subject: str | None) -> str:
    """A clean event title from the reply subject: drop leading Re:/Fwd: noise."""
    s = (subject or "").strip()
    while True:
        m = re.match(r"^(re|fwd|fw)\s*:\s*", s, flags=re.IGNORECASE)
        if not m:
            break
        s = s[m.end():].strip()
    return s or "Meeting"


def _format_slot(start_iso: str, end_iso: str) -> str:
    """Human-friendly 'Tue Jan 01, 2:00–2:30 PM' for the prompt; falls back to
    the raw ISO strings if either can't be parsed."""
    try:
        s = datetime.fromisoformat(start_iso)
        e = datetime.fromisoformat(end_iso)
    except (TypeError, ValueError):
        return f"{start_iso} – {end_iso}"
    return f"{s:%a %b %d, %-I:%M}–{e:%-I:%M %p}"


def _resolve_complete_fn(model: str, *, local_tokens: int = _MAX_TOKENS_LOCAL, cloud_tokens: int = _MAX_TOKENS_CLOUD):
    """Pick the completion fn for the configured tier (with a tier-appropriate
    token budget), falling back to local if a requested ``cloud`` model is
    unavailable — so recall never silently drops to zero in a headless/cron run
    with no Claude auth. Returns None only when nothing is available."""
    from app.core.completion import select_completion

    tier = (model or "cloud").strip().lower()
    budget = cloud_tokens if tier == "cloud" else local_tokens
    fn = select_completion(tier, max_tokens=budget, temperature=0.0)
    if fn is None and tier != "local":
        # Requested tier (e.g. cloud) unavailable → degrade to the on-device model.
        fn = select_completion("local", max_tokens=local_tokens, temperature=0.0)
    return fn


def detect_confirmation(
    *,
    subject: str | None,
    sender: str | None,
    sender_email: str | None,
    body: str | None,
    proposed_slots: list,
    account_emails: set[str] | None = None,
    complete_fn=None,
    model: str = "cloud",
) -> ConfirmationResult | None:
    """Ask a model whether the reply confirms one of ``proposed_slots``.

    ``proposed_slots`` is ``[[start_iso, end_iso], ...]`` (as persisted by b265).
    Returns a :class:`ConfirmationResult` for an unambiguous single-slot
    acceptance, else ``None``. ``complete_fn`` is injectable for tests; otherwise
    ``model`` selects the tier — ``'cloud'`` (Claude, stronger recall on terse
    acceptances; sends the reply text off-device) or ``'local'`` (on-device,
    no egress). An unavailable ``cloud`` tier degrades to ``local``."""
    slots = [s for s in (proposed_slots or []) if isinstance(s, (list, tuple)) and len(s) == 2]
    if not slots:
        return None

    if complete_fn is None:
        complete_fn = _resolve_complete_fn(model)
        if complete_fn is None:
            return None

    snippet = " ".join((body or "").split())[:_BODY_SNIPPET_CHARS]
    n = len(slots)
    listing = "\n".join(
        f"{i + 1}. {_format_slot(s[0], s[1])}" for i, s in enumerate(slots)
    )
    prompt = (
        f"I proposed {n} meeting time(s) to someone:\n"
        f"{listing}\n\n"
        "Did their reply AGREE to meet at one of these proposed times? When only "
        "one time was proposed, any clear acceptance counts — including a short "
        "one like 'perfect', 'great', 'confirmed', 'yes', 'works for me', 'see "
        "you then', 'add me', a thumbs-up, or saying they'll send/accept a "
        "calendar invite. Answer NONE only if they asked for a DIFFERENT time, "
        "declined, made it conditional, asked a question, or did not commit to a "
        "specific time.\n"
        "End your answer with a line exactly like 'FINAL: <slot number>' or "
        "'FINAL: NONE'.\n\n"
        f"From: {sender or sender_email or '(unknown)'}\n"
        f"Reply: {snippet}\n\n"
        "Answer:"
    )
    try:
        out = complete_fn(prompt)
    except Exception as exc:
        logger.info("meeting-confirm detection skipped (model unavailable): %s", exc)
        return None

    idx = _parse_choice(out or "", len(slots))
    if idx is None:
        return None

    start_iso, end_iso = slots[idx][0], slots[idx][1]
    acct = {e.lower() for e in (account_emails or set())}
    attendees: list[str] = []
    if sender_email and sender_email.lower() not in acct:
        attendees.append(sender_email)
    return ConfirmationResult(
        start_iso=str(start_iso),
        end_iso=str(end_iso),
        title=_strip_re(subject),
        attendees=attendees,
        confidence=0.85,
        reasons=[f"reply confirmed proposed slot #{idx + 1} ({_format_slot(start_iso, end_iso)})"],
    )


def _parse_self_datetime(out: str, tz: str) -> tuple[str, str] | None:
    """Parse the model's ``FINAL: <ISO datetime>|NONE`` line into
    ``(start_iso, end_iso)`` (end = start + 30 min), or None. A naive datetime
    (no offset) is localized to ``tz``. Never mines a date out of free prose —
    only the FINAL line counts."""
    t = (out or "").strip()
    m = re.search(r"final\s*:\s*(.+)$", t, flags=re.IGNORECASE | re.MULTILINE)
    if not m:
        return None
    val = m.group(1).strip()
    if val.lower().startswith("none"):
        return None
    iso = _ISO_DT_RE.search(val)
    if not iso:
        return None
    raw = iso.group(0).replace(" ", "T", 1).replace(" ", "")  # "2026-06-24 14:00" → "2026-06-24T14:00"; tighten "+02:00"
    try:
        start = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if start.tzinfo is None:
        try:
            from zoneinfo import ZoneInfo

            start = start.replace(tzinfo=ZoneInfo(tz))
        except Exception:
            return None  # can't anchor a timezone → don't queue an ambiguous event
    end = start + timedelta(minutes=_DEFAULT_MEETING_MINUTES)
    return start.isoformat(), end.isoformat()


def detect_self_scheduled(
    *,
    subject: str | None,
    body: str | None,
    recipients: list[str] | None,
    account_emails: set[str] | None = None,
    now_iso: str | None = None,
    tz: str = "UTC",
    complete_fn=None,
    model: str = "cloud",
) -> ConfirmationResult | None:
    """Detect that the USER's own sent message confirms/proposes a SPECIFIC
    meeting time, and extract it — the other direction from
    :func:`detect_confirmation` (which watches the counterparty accept OUR
    proposal). Catches a meeting you arranged yourself (incl. a manual Gmail
    reply), so YouOS can queue the calendar invite for approval.

    ``body`` should be the new (quote-stripped) content of the user's message;
    ``recipients`` are the people it was sent to (the attendees). Returns a
    :class:`ConfirmationResult` (confidence 0.7 — an extracted datetime is less
    certain than picking a proposed slot, and it's approval-gated anyway) only
    when a concrete date+time is found AND there is at least one attendee. Vague
    ("sometime next week"), no-time, or non-meeting messages → None."""
    acct = {e.lower() for e in (account_emails or set())}
    attendees = [r for r in (recipients or []) if r and r.lower() not in acct]
    if not attendees:
        return None  # an invite needs someone to invite

    if complete_fn is None:
        complete_fn = _resolve_complete_fn(
            model, local_tokens=_MAX_TOKENS_SELF_LOCAL, cloud_tokens=_MAX_TOKENS_SELF_CLOUD
        )
        if complete_fn is None:
            return None

    snippet = " ".join((body or "").split())[:_BODY_SNIPPET_CHARS]
    # Cheap gate: no clock time / scheduling word → not a meeting confirmation;
    # don't spend a model call (and can't extract a time anyway).
    if not _MEETING_SIGNAL_RE.search(snippet):
        return None
    prompt = (
        f"Today is {now_iso or '(unknown)'}. The user's timezone is {tz}.\n"
        "Below is a message the USER just sent. Does it CONFIRM or PROPOSE a "
        "SPECIFIC meeting date AND clock time (an exact day + time, e.g. "
        "'Tuesday at 2pm', 'June 24 14:00') — not a vague 'sometime next week'?\n"
        "If yes, resolve it to an absolute datetime and end with a line exactly "
        f"like 'FINAL: <ISO8601 start datetime with the {tz} offset>'.\n"
        "If it is vague, has no specific time, declines, or is not about "
        "scheduling a meeting, end with 'FINAL: NONE'.\n\n"
        f"Message: {snippet}\n\nAnswer:"
    )
    try:
        out = complete_fn(prompt)
    except Exception as exc:
        logger.info("self-scheduled detection skipped (model unavailable): %s", exc)
        return None

    parsed = _parse_self_datetime(out or "", tz)
    if parsed is None:
        return None
    start_iso, end_iso = parsed
    return ConfirmationResult(
        start_iso=start_iso,
        end_iso=end_iso,
        title=_strip_re(subject),
        attendees=attendees,
        confidence=0.7,
        reasons=[f"you confirmed a meeting time in your reply ({_format_slot(start_iso, end_iso)})"],
    )
