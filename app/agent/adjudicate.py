"""Borderline LLM adjudication — a broadcast/personal veto on uncertain mail.

The needs-reply classifier (``needs_reply.py``) is a fast additive heuristic.
On scores just over the threshold it can't reliably tell a personal note that
happens to look automated from a broadcast that happens to look personal — the
live false positives ("thanks, I'll check it out" to a newsletter) live exactly
in this band. The warm local model is right there, so for would-be drafts in a
narrow band we ask it one constrained question: is this a PERSONAL message that
expects a reply from me, or a BROADCAST (newsletter / marketing / automated)?

Adjudication only ever **vetoes** — it can demote a borderline draft to
surface-for-review, never promote a message the heuristic rejected. On-device
(no egress); failure-isolated — model unavailable or an unparseable answer ⇒ no
veto, the heuristic stands.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Only adjudicate would-be drafts whose score is *below* this — the least
# certain passes, just over the threshold, where the heuristic most often lets
# a broadcast through. Above it the heuristic verdict is trusted as-is.
DEFAULT_HIGH = 0.8

_BODY_SNIPPET_CHARS = 600
_MAX_TOKENS = 8

_BROADCAST_WORDS = frozenset(
    {"broadcast", "automated", "newsletter", "marketing", "promotional", "mass"}
)
_PERSONAL_WORDS = frozenset({"personal", "human", "individual"})


@dataclass
class AdjudicationResult:
    verdict: str        # "personal" | "broadcast" | "unknown"
    is_broadcast: bool
    raw: str


def _parse(out: str) -> AdjudicationResult:
    """Map the model's free-text answer to a verdict. The prompt asks for one
    word, but we tolerate a leading word or an answer that merely *mentions*
    the class. Anything we can't read confidently is ``unknown`` (no veto)."""
    t = (out or "").strip().lower()
    if not t:
        return AdjudicationResult("unknown", False, out or "")
    head = re.split(r"[^a-z]+", t, maxsplit=1)[0]
    if head in _BROADCAST_WORDS:
        return AdjudicationResult("broadcast", True, out)
    if head in _PERSONAL_WORDS:
        return AdjudicationResult("personal", False, out)
    has_b = any(w in t for w in _BROADCAST_WORDS)
    has_p = any(w in t for w in _PERSONAL_WORDS)
    if has_b and not has_p:
        return AdjudicationResult("broadcast", True, out)
    if has_p and not has_b:
        return AdjudicationResult("personal", False, out)
    return AdjudicationResult("unknown", False, out)


def adjudicate(
    *,
    subject: str | None,
    sender: str | None,
    body: str | None,
    complete_fn=None,
    max_tokens: int = _MAX_TOKENS,
) -> AdjudicationResult | None:
    """Ask the warm model whether this email is personal or a broadcast.

    Returns ``None`` when the model is unavailable (caller keeps its heuristic
    verdict). ``complete_fn`` is injectable for tests; by default it routes to
    the warm local model server at temperature 0 (deterministic)."""
    if complete_fn is None:
        from app.core import model_server

        if not model_server.is_enabled():
            return None

        def complete_fn(p: str) -> str:
            return model_server.complete(p, max_tokens=max_tokens, temperature=0.0)

    snippet = " ".join((body or "").split())[:_BODY_SNIPPET_CHARS]
    prompt = (
        "You are triaging an inbox. Decide whether this email is a PERSONAL "
        "message from a human that expects a reply from me, or a BROADCAST "
        "(newsletter, marketing, automated notification, or mass mail) that "
        "does not. Answer with exactly one word: PERSONAL or BROADCAST.\n\n"
        f"From: {sender or '(unknown)'}\n"
        f"Subject: {subject or '(none)'}\n"
        f"Body: {snippet}\n\n"
        "Answer:"
    )
    try:
        out = complete_fn(prompt)
    except Exception as exc:
        logger.info("adjudication skipped (model unavailable): %s", exc)
        return None
    return _parse(out or "")
