"""Cold-outbound (sales / cold-outreach) detection.

QA found the LoRA politely accepts pushy outbound emails it shouldn't —
Baher's training data doesn't include many polite-decline replies (he
mostly ignores cold sales), so the model defaults to cooperative. This
module catches the *inbound* shape so generation can nudge the prompt
toward a polite decline.

Implementation: weighted heuristic. Each pattern hit counts; threshold
classifies as cold-outreach. The goal isn't perfect precision — it's
"flag the clearly-pushy cases so generation knows not to over-commit."
Caller decides what to do with the flag.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Subject-line cold-pitch language. Each pattern fires only when the subject
# uses *outbound* framing (selling/growing/optimising the recipient).
SUBJECT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(boost|grow|scale|10x|explode|unlock|supercharge)\b", re.IGNORECASE),
    re.compile(r"\bquick\s+(?:chat|call|question|sync)\b", re.IGNORECASE),
    re.compile(r"\b\d+[\s-]?min(?:ute)?s?\s+(?:call|chat|sync)\b", re.IGNORECASE),
    re.compile(r"\b(?:demo|intro|partnership|opportunity)\?", re.IGNORECASE),
)

# Body cold-pitch language. The single strongest signal is the templated
# "I work with [type] founders/CEOs/companies" / "I help [X] do [Y]" framing.
BODY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\bI\s+(?:work with|help|serve|partner with|support|assist)\s+[\w\s,/-]{0,40}?"
        r"\b(?:founders|CEOs|companies|startups|SaaS|teams|brands)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:saw|noticed|came across|stumbled on)\s+(?:your|youos|your company|your post)\b", re.IGNORECASE),
    re.compile(r"\bcan\s+I\s+(?:steal|grab|borrow|book)\s+(?:\d+|a\s+few)\s*min", re.IGNORECASE),
    re.compile(r"\bjust\s+(?:\d+|a\s+few|some)\s+(?:min|minutes)\b", re.IGNORECASE),
    re.compile(r"\b10x\b", re.IGNORECASE),
    re.compile(r"\b(?:our\s+)?portfolio\s+(?:founders|companies|clients)\b", re.IGNORECASE),
    re.compile(r"\b(?:open\s+to|interested\s+in)\s+(?:a\s+)?(?:quick\s+)?call\b", re.IGNORECASE),
)

# Domain heuristics. Marketing/growth/outreach SaaS often have these words
# in the domain. False positives are possible (a legitimate "growth team"
# might use a "growth" domain) — that's why we require multiple signals
# overall, not just domain alone.
DOMAIN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"@[\w.-]*(?:market|growth|outreach|sales|leads?gen|saleshero)[\w.-]*\.", re.IGNORECASE),
)

# A high-confidence body pattern: the literal "I work with…founders" template
# is so specific to cold outreach that it counts as TWO signals.
_HIGH_CONFIDENCE_BODY = BODY_PATTERNS[0]

# Threshold for the boolean verdict. 3 signals = "clearly cold pitch." Tuned
# so the Jess QA case fires (subject "Boost…30-min call" + body "I work
# with…founders" + body "10x" + body "steal 30 min" = 4) but a legitimate
# work email asking for a quick call doesn't.
COLD_OUTBOUND_THRESHOLD = 3


@dataclass(frozen=True)
class ColdOutboundVerdict:
    is_cold: bool
    score: int
    hits: tuple[str, ...]


def detect_cold_outbound(
    *,
    subject: str | None,
    body: str | None,
    sender_email: str | None,
) -> ColdOutboundVerdict:
    """Run the heuristic. Returns the boolean verdict, the raw score, and the
    list of pattern names that fired (useful for testing + audit logging)."""
    hits: list[str] = []
    score = 0

    if subject:
        for i, pat in enumerate(SUBJECT_PATTERNS):
            if pat.search(subject):
                hits.append(f"subject:{i}")
                score += 1

    if body:
        for i, pat in enumerate(BODY_PATTERNS):
            if pat.search(body):
                hits.append(f"body:{i}")
                # Templated "I work with founders" is the smoking gun —
                # weight it double.
                score += 2 if pat is _HIGH_CONFIDENCE_BODY else 1

    if sender_email:
        for i, pat in enumerate(DOMAIN_PATTERNS):
            if pat.search(sender_email):
                hits.append(f"domain:{i}")
                score += 1

    return ColdOutboundVerdict(
        is_cold=score >= COLD_OUTBOUND_THRESHOLD,
        score=score,
        hits=tuple(hits),
    )


# The prompt nudge appended when cold outreach is detected. Phrased as
# guidance, not a hard rule — the LoRA is a small model and rigid
# instructions may not survive its decoding.
DECLINE_NUDGE = (
    "Note: this looks like an unsolicited cold outreach from someone you "
    "don't have prior history with. Reply briefly and either politely "
    "decline or ask a clarifying question — do not commit to a call or "
    "share materials by default. Keep it short."
)
