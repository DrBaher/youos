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
from datetime import datetime

logger = logging.getLogger(__name__)

_BODY_SNIPPET_CHARS = 600
_MAX_TOKENS = 8


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


def _parse_choice(out: str, n_slots: int) -> int | None:
    """Map the model's answer to a 0-based slot index, or None (no acceptance).

    The prompt asks for the 1-based slot number or NONE; we tolerate a leading
    word and only accept a number in range. Anything else is None (no event)."""
    t = (out or "").strip().lower()
    if not t or t.startswith("none"):
        return None
    # Anchor on a LEADING number (optionally after "slot/option/number/#"). We
    # deliberately do NOT search the whole string: a model that ignores the
    # "answer with only the number" instruction and explains itself ("the person
    # did NOT confirm, so 1 would be wrong") must not have a stray digit mined
    # out as a confirmation — that would create a wrong event. Prose ⇒ None.
    m = re.match(r"(?:slot|option|number|no\.?)?\s*#?\s*(\d+)\b", t)
    if not m:
        return None
    n = int(m.group(1))
    if 1 <= n <= n_slots:
        return n - 1
    return None


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


def detect_confirmation(
    *,
    subject: str | None,
    sender: str | None,
    sender_email: str | None,
    body: str | None,
    proposed_slots: list,
    account_emails: set[str] | None = None,
    complete_fn=None,
    max_tokens: int = _MAX_TOKENS,
) -> ConfirmationResult | None:
    """Ask the warm model whether the reply confirms one of ``proposed_slots``.

    ``proposed_slots`` is ``[[start_iso, end_iso], ...]`` (as persisted by b265).
    Returns a :class:`ConfirmationResult` for an unambiguous single-slot
    acceptance, else ``None``. ``complete_fn`` is injectable for tests; by
    default it routes to the warm local model server at temperature 0."""
    slots = [s for s in (proposed_slots or []) if isinstance(s, (list, tuple)) and len(s) == 2]
    if not slots:
        return None

    if complete_fn is None:
        from app.core import model_server

        if not model_server.is_enabled():
            return None

        def complete_fn(p: str) -> str:
            return model_server.complete(p, max_tokens=max_tokens, temperature=0.0)

    snippet = " ".join((body or "").split())[:_BODY_SNIPPET_CHARS]
    listing = "\n".join(
        f"{i + 1}. {_format_slot(s[0], s[1])}" for i, s in enumerate(slots)
    )
    prompt = (
        "I earlier offered someone these meeting times:\n"
        f"{listing}\n\n"
        "Here is their reply. Decide whether they clearly CONFIRMED exactly one "
        "of those exact times (e.g. 'Tuesday 2pm works', 'let's do the first "
        "one', 'see you then'). If they did, answer with that slot's NUMBER. If "
        "they proposed a different time, declined, were vague, or asked a "
        "question, answer NONE. Answer with only the number or NONE.\n\n"
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
