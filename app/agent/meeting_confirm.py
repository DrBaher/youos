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
from datetime import datetime, timedelta, timezone

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
# Counterparty-availability extraction emits an ISO datetime OR a start..end
# range on its FINAL line, so it needs the most room of the terse paths.
_MAX_TOKENS_AVAIL_LOCAL = 96
_MAX_TOKENS_AVAIL_CLOUD = 256
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
# Scheduling-INTENT gate for Direction C (b287). A counterparty stating a time
# to meet has an unambiguous scheduling phrase — a clock time, or a
# meeting/availability word — NOT just an incidental weekday/week word (a
# newsletter's "this week's edition" or a "Sunday" dateline). Requiring this to
# even call the model is what stops bogus invites off newsletter prose (a live
# b287 bug: Monocle/AgentNews/digests queued invites). The needs-reply gate in
# triage (hard-skipped newsletters never reach here) is the primary defense;
# this is the detector-level backstop for any human 1:1 mail that slips through.
_SCHEDULE_INTENT_RE = re.compile(
    r"\b\d{1,2}\s*(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b|"
    r"\b(?:meet|meeting|schedul\w*|reschedul\w*|sync|catch[\s-]?up|appointment|"
    r"calendar|invite|works\s+(?:for|on)|good\s+(?:for|on)|that\s+works|"
    r"suits?\s+(?:me|you|us)|my\s+end|your\s+end|available|availabl\w*|free|"
    r"i'?m\s+(?:free|available|around|good|flexible|open)|i\s+can\s+(?:do|make)|"
    r"book\s+a|set\s+up\s+a|hop\s+on|when\s+(?:works|are\s+you|suits)|what\s+time|slot|"
    r"let'?s\s+(?:meet|schedule|sync|find|do|set|grab|hop|catch|talk|chat|connect))\b",
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


@dataclass
class AvailabilityResult:
    """A time or window the COUNTERPARTY stated in an inbound email.

    ``kind`` is ``"specific"`` (they named an exact day+time) or ``"range"``
    (they gave a window like "next week" / "Tuesday or Wednesday afternoon").
    For ``specific`` the ``[start_iso, end_iso]`` IS the meeting; for ``range``
    it is the WINDOW the caller must intersect with the user's free/busy to pick
    one concrete slot. All ISO strings carry the ``tz`` offset."""

    kind: str
    start_iso: str
    end_iso: str
    title: str
    attendees: list[str]
    confidence: float
    reasons: list[str]


def _iso_to_dt(raw: str, tz: str) -> datetime | None:
    """Parse one ``_ISO_DT_RE`` match into a tz-aware datetime (naive → ``tz``)."""
    try:
        dt = datetime.fromisoformat(raw.replace(" ", "T", 1))
    except ValueError:
        return None
    if dt.tzinfo is None:
        try:
            from zoneinfo import ZoneInfo

            dt = dt.replace(tzinfo=ZoneInfo(tz))
        except Exception:
            return None
    return dt


def _parse_availability(out: str, tz: str, meeting_minutes: int) -> tuple[str, str, str] | None:
    """Parse the model's ``FINAL:`` line into ``(kind, start_iso, end_iso)``.

    Accepts ``FINAL: AT <iso>`` (specific → end = start + meeting_minutes),
    ``FINAL: RANGE <iso>..<iso>`` (window), or ``FINAL: NONE``. Only the FINAL
    line counts — never mines a date out of free prose. Fail-safe: any missing/
    unparseable/backwards value → None (the local model is told to answer NONE
    when unsure, and this backstops it)."""
    t = (out or "").strip()
    m = re.search(r"final\s*:\s*(.+)$", t, flags=re.IGNORECASE | re.MULTILINE)
    if not m:
        return None
    val = m.group(1).strip()
    low = val.lower()
    if low.startswith("none"):
        return None
    if low.startswith("range"):
        isos = _ISO_DT_RE.findall(val)
        if len(isos) < 2:
            return None
        start = _iso_to_dt(isos[0], tz)
        end = _iso_to_dt(isos[1], tz)
        if start is None or end is None or end <= start:
            return None
        return "range", start.isoformat(), end.isoformat()
    iso = _ISO_DT_RE.search(val)
    if not iso:
        return None
    start = _iso_to_dt(iso.group(0), tz)
    if start is None:
        return None
    end = start + timedelta(minutes=meeting_minutes)
    return "specific", start.isoformat(), end.isoformat()


def _deterministic_range(text: str, now_iso: str | None, tz: str) -> tuple[str, str, str] | None:
    """On-device, model-free resolution of common availability phrases into a
    day-level window — the private fallback for when the local model misses a
    range ("next week", "this week", "tomorrow", a weekday name). Returns
    ``("range", start_iso, end_iso)`` (whole days; the caller's free/busy +
    work-hours pass picks the actual time) or None. Conservative: only fires on
    an unambiguous phrase, so it can't invent a meeting from vague prose."""
    from datetime import time as _time
    from zoneinfo import ZoneInfo

    from app.agent.calendar import _WEEKDAY_NUMS

    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = timezone.utc
    now_local = None
    if now_iso:
        try:
            now_local = datetime.fromisoformat(now_iso.replace("Z", "+00:00")).astimezone(zone)
        except ValueError:
            now_local = None
    if now_local is None:
        now_local = datetime.now(zone)
    today = now_local.date()
    low = " ".join((text or "").lower().split())

    def _window(d0, d1):
        s = datetime.combine(d0, _time(0, 0), tzinfo=zone)
        e = datetime.combine(d1, _time(23, 59), tzinfo=zone)
        return "range", s.isoformat(), e.isoformat()

    if "next week" in low:
        mon = today + timedelta(days=(7 - today.weekday()))  # next Monday
        return _window(mon, mon + timedelta(days=4))          # Mon–Fri
    if "this week" in low:
        fri = today + timedelta(days=(4 - today.weekday()))
        if fri < today:  # it's the weekend already → roll to next week
            mon = today + timedelta(days=(7 - today.weekday()))
            return _window(mon, mon + timedelta(days=4))
        return _window(today, fri)
    if re.search(r"\btomorrow\b", low):
        d = today + timedelta(days=1)
        return _window(d, d)
    # Explicit weekday(s): span the earliest to the latest mentioned, so "Tuesday
    # and Wed" becomes a Tue–Wed window (the availability + preferred-weekday pass
    # then picks whichever day is open), not just the first weekday.
    wd_dates = []
    for name, wd in _WEEKDAY_NUMS.items():
        if re.search(rf"\b{name}\b", low):
            days = (wd - today.weekday()) % 7 or 7  # the NEXT such weekday (not today)
            wd_dates.append(today + timedelta(days=days))
    if wd_dates:
        return _window(min(wd_dates), max(wd_dates))
    return None


def detect_counterparty_availability(
    *,
    subject: str | None,
    sender: str | None,
    sender_email: str | None,
    body: str | None,
    account_emails: set[str] | None = None,
    now_iso: str | None = None,
    tz: str = "UTC",
    meeting_minutes: int = _DEFAULT_MEETING_MINUTES,
    complete_fn=None,
    model: str = "local",
) -> AvailabilityResult | None:
    """Detect that the COUNTERPARTY's inbound states a meeting time or a window
    of availability, and extract it — the third direction (Direction C), for the
    common case where THEY confirm/propose a time and we never proposed slots
    (so :func:`detect_confirmation` has nothing to match).

    Returns an :class:`AvailabilityResult` (``kind`` ``specific``/``range``) or
    ``None``. The caller intersects a ``range`` with the user's free/busy to pick
    one slot. ``model`` defaults to ``'local'`` (on-device, no egress) — the
    prompt tells it to answer NONE when unsure, so a weak parse fails safe rather
    than inventing a meeting. Requires ≥1 attendee (the sender). Failure-isolated."""
    acct = {e.lower() for e in (account_emails or set())}
    attendees: list[str] = []
    if sender_email and sender_email.lower() not in acct:
        attendees.append(sender_email)
    if not attendees:
        return None  # an invite needs someone to invite

    snippet = " ".join((body or "").split())[:_BODY_SNIPPET_CHARS]
    # Gate: a real scheduling phrase (clock time / meeting / availability word),
    # not just an incidental weekday. Keeps the model (and the range fallback)
    # away from newsletter prose that merely mentions "this week"/"Sunday".
    if not _SCHEDULE_INTENT_RE.search(snippet):
        return None

    if complete_fn is None:
        complete_fn = _resolve_complete_fn(
            model, local_tokens=_MAX_TOKENS_AVAIL_LOCAL, cloud_tokens=_MAX_TOKENS_AVAIL_CLOUD
        )
        if complete_fn is None:
            return None

    prompt = (
        f"Today is {now_iso or '(unknown)'}. The user's timezone is {tz}.\n"
        "Below is an email the user RECEIVED. Does the sender state a time to "
        "meet or their availability?\n"
        "- If they name a SPECIFIC date and clock time (e.g. 'Thursday at 3pm', "
        f"'June 24 14:00'), resolve it to an absolute datetime with the {tz} "
        "offset and end with a line exactly like 'FINAL: AT <ISO8601 datetime>'.\n"
        "- If they give a RANGE or window (e.g. 'next week', 'Tuesday or "
        "Wednesday', 'any afternoon Thursday', 'free after 2pm Friday'), resolve "
        "it to start and end datetimes and end with 'FINAL: RANGE <start ISO>.."
        "<end ISO>'.\n"
        "- If there is no clear time/range, or it is not about meeting, end with "
        "'FINAL: NONE'.\n"
        "Only commit to what you are sure of; when in doubt answer 'FINAL: NONE'.\n\n"
        f"From: {sender or sender_email or '(unknown)'}\n"
        f"Email: {snippet}\n\nAnswer:"
    )
    try:
        out = complete_fn(prompt)
    except Exception as exc:
        logger.info("counterparty-availability detection skipped (model unavailable): %s", exc)
        return None

    parsed = _parse_availability(out or "", tz, meeting_minutes)
    if parsed is None:
        # Local models reliably resolve a SPECIFIC time but miss ranges ("next
        # week"); a deterministic on-device fallback recovers the common range
        # phrases without egress (keeps the 'local' privacy choice intact). Safe
        # here — the scheduling-intent gate above already ran.
        parsed = _deterministic_range(snippet, now_iso, tz)
    if parsed is None:
        return None
    kind, start_iso, end_iso = parsed
    if kind == "specific":
        conf = 0.7
        reasons = [f"they confirmed a specific time ({_format_slot(start_iso, end_iso)})"]
    else:
        conf = 0.6
        reasons = [f"they're available {_format_slot(start_iso, end_iso)}"]
    return AvailabilityResult(
        kind=kind,
        start_iso=start_iso,
        end_iso=end_iso,
        title=_strip_re(subject),
        attendees=attendees,
        confidence=conf,
        reasons=reasons,
    )
