"""Cold-outbound (sales / cold-outreach) detection.

QA found the LoRA politely accepts pushy outbound emails it shouldn't —
Baher's training data doesn't include many polite-decline replies (he
mostly ignores cold sales), so the model defaults to cooperative. This
module catches the *inbound* shape so generation can nudge the prompt
toward a polite decline, and so triage can surface (not auto-draft) the
clearly-cold pitches.

Implementation: weighted heuristic over categories. Each pattern hit counts;
``is_cold`` (score >= threshold) drives the generation decline-nudge and a
soft score penalty. ``confident`` additionally requires a *strong* category
(investor/M&A vocabulary, a sales-engagement tracking link, an individual
"reply unsub" opt-out, a savings pitch, or the templated "I work with
founders") — generic subject/intro language alone never makes it confident.
The caller (needs_reply.classify) only demotes a thread to never-draft when
``confident`` AND there's no prior reply history with the sender (a genuine
first-contact pitch, not an established correspondent).

Patterns were grounded in a 2026-06 live-corpus review (investor M&A intros,
capital-raise pitches, sales-engagement blasts) — not just the original QA case.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Subject-line cold-pitch language (outbound framing: selling/growing the
# recipient, or a low-commitment "quick call / N-min sync / meet up").
SUBJECT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(boost|grow|scale|10x|explode|unlock|supercharge)\b", re.IGNORECASE),
    re.compile(r"\bquick\s+(?:chat|call|question|sync)\b", re.IGNORECASE),
    re.compile(r"\b\d+[\s-]?min(?:ute)?s?\s+(?:call|chat|sync)\b", re.IGNORECASE),
    re.compile(r"\b(?:demo|intro|introduction|partnership|opportunity)\b", re.IGNORECASE),
    re.compile(r"\bmeet[\s-]?up\b", re.IGNORECASE),
)

# First-contact / self-introduction framing (EN + DE — the live corpus had
# German M&A intros). On its own this is weak; it pairs with a strong category.
INTRO_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:let me|allow me to|I(?:'d| would) like to|I wanted to)\s+(?:briefly\s+)?introduce\b", re.IGNORECASE),
    re.compile(r"\bI(?:'m| am)\s+reaching\s+out\b", re.IGNORECASE),
    re.compile(r"\b(?:I\s+)?(?:recently\s+)?(?:came across|stumbled (?:up)?on|saw|noticed)\s+(?:your|you|youos|medicus|\[?company)", re.IGNORECASE),
    re.compile(r"\bI\s+understand\s+(?:that\s+)?you\s+(?:are|might|may|want)", re.IGNORECASE),
    re.compile(r"\bmy\s+name\s+is\b", re.IGNORECASE),
    # German: "möchte mich (Ihnen) (kurz) vorstellen", "ich bin … auf … gestoßen"
    re.compile(r"\bm(?:ö|oe)chte\s+mich\b.*\bvorstellen\b", re.IGNORECASE),
    re.compile(r"\bbin\s+(?:vor\s+kurzem\s+)?auf\b.*\bgesto(?:ß|ss)en\b", re.IGNORECASE),
)

# Body cold-pitch templates (the original QA signals).
BODY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\bI\s+(?:work with|help|serve|partner with|support|assist)\s+[\w\s,/-]{0,40}?"
        r"\b(?:founders|CEOs|companies|startups|SaaS|teams|brands)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bcan\s+I\s+(?:steal|grab|borrow|book)\s+(?:\d+|a\s+few)\s*min", re.IGNORECASE),
    re.compile(r"\bjust\s+(?:\d+|a\s+few|some)\s+(?:min|minutes)\b", re.IGNORECASE),
    re.compile(r"\b10x\b", re.IGNORECASE),
    re.compile(r"\b(?:our\s+)?portfolio\s+(?:founders|companies|clients)\b", re.IGNORECASE),
    re.compile(r"\b(?:open\s+to|interested\s+in)\s+(?:a\s+)?(?:quick\s+)?call\b", re.IGNORECASE),
)

# STRONG: investor / M&A / capital-raise vocabulary (EN + DE). A cold investor
# or banker intro is the dominant live false-positive class.
INVESTOR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\braise\s+(?:capital|a\s+round|funding)\b", re.IGNORECASE),
    re.compile(r"\b(?:capital\s+raise|fundrais(?:e|ing)|capital\s+participation|term\s+sheet|ticket\s+size)\b", re.IGNORECASE),
    re.compile(r"\bpitch\s+deck\b", re.IGNORECASE),
    re.compile(r"\bM&A\b|\bmergers?\s+(?:and|&)\s+acquisitions?\b|\bacquisition\b", re.IGNORECASE),
    re.compile(r"\binvestment\s+(?:bank|manager|opportunity|firm)\b|\binvestmentbank\b", re.IGNORECASE),
    re.compile(r"\bKapitalbeschaffung\b|\bKapital\s+beschaffen\b", re.IGNORECASE),
    re.compile(
        r"\b(?:we|our\s+(?:firm|fund))\s+(?:might\s+be\s+|would\s+be\s+)?"
        r"interested\s+in\s+(?:participating|investing|this\s+round)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:advised|mandate|originated\s+the\s+transaction|deploy\s+capital)\b", re.IGNORECASE),
    re.compile(r"\bventure\s+capital\b|\bprivate\s+equity\b", re.IGNORECASE),
    re.compile(r"\b(?:closing|close)\s+(?:the\s+)?(?:next\s+)?round\b|\bnext\s+(?:funding\s+)?round\b", re.IGNORECASE),
)

# STRONG: an explicit cost/ROI savings pitch ("cut costs by 30-50%", "save up
# to 40%").
SAVINGS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:cut|cuts|save|saving|reduce|lower)\b[\w\s,./-]{0,30}?\b(?:cost|costs|spend|price|bill)\b[\w\s,./-]{0,20}?\b\d{1,3}\s*%", re.IGNORECASE),
    re.compile(r"\b\d{1,3}\s*-\s*\d{1,3}\s*%\s+(?:off|cheaper|savings?)\b", re.IGNORECASE),
)

# STRONG: an *individual* opt-out ("reply unsub", "I'll never bug you again",
# "not for you?") — the calling card of a 1:1 sales blast (distinct from the
# List-Unsubscribe HEADER, which hard-skips earlier as a bulk newsletter).
UNSUB_INDIVIDUAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\breply\s+[\"']?unsub", re.IGNORECASE),
    re.compile(r"\bI(?:'ll| will)\s+never\s+(?:bug|bother|email|contact)\s+you\s+again\b", re.IGNORECASE),
    re.compile(r"\bnot\s+for\s+you\?", re.IGNORECASE),
    re.compile(r"\b(?:just\s+)?let\s+me\s+know\s+if\s+you(?:'d| would)?\s+(?:prefer|rather)\s+(?:not|to\s+opt)", re.IGNORECASE),
    re.compile(r"\breply\s+stop\s+to\s+opt\s*-?\s*out\b", re.IGNORECASE),
)

# STRONG: sales-engagement / mail-merge tracking domains in body links (or the
# sender). These platforms exist only for cold outbound at scale.
SALES_TRACKING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:hs-sales-engage|salesloft|outreach\.io|lemlist|apollo\.io|yamm-track|"
        r"mailtrack|woodpecker|instantly\.ai|smartlead|reply\.io|mixmax)\b",
        re.IGNORECASE,
    ),
)

# Marketing/growth/outreach SaaS domains (weak — pairs with other signals).
DOMAIN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"@[\w.-]*(?:market|growth|outreach|sales|leads?gen|saleshero|capital|ventures?|partners|invest)[\w.-]*\.", re.IGNORECASE),
)

# The templated "I work with…founders" body line is so specific it weighs double.
_HIGH_CONFIDENCE_BODY = BODY_PATTERNS[0]

# is_cold threshold (drives the soft penalty + generation decline-nudge).
COLD_OUTBOUND_THRESHOLD = 3


@dataclass(frozen=True)
class ColdOutboundVerdict:
    is_cold: bool
    score: int
    hits: tuple[str, ...]
    # confident = is_cold AND a strong category fired. The caller demotes a
    # thread to never-draft on (confident AND no prior history with the sender).
    confident: bool = False


def detect_cold_outbound(
    *,
    subject: str | None,
    body: str | None,
    sender_email: str | None,
) -> ColdOutboundVerdict:
    """Run the heuristic. Returns the boolean verdict, the raw score, the list
    of pattern names that fired (for testing/audit), and ``confident``."""
    hits: list[str] = []
    score = 0
    strong = False  # at least one investor / tracking / unsub / savings / template hit

    def _scan(text: str, patterns, label: str, *, weight: int = 1, is_strong: bool = False) -> None:
        nonlocal score, strong
        for i, pat in enumerate(patterns):
            if pat.search(text):
                hits.append(f"{label}:{i}")
                score += weight
                if is_strong:
                    strong = True

    if subject:
        _scan(subject, SUBJECT_PATTERNS, "subject")

    if body:
        # Templated "I work with founders" is the smoking gun — weight double + strong.
        for i, pat in enumerate(BODY_PATTERNS):
            if pat.search(body):
                hits.append(f"body:{i}")
                if pat is _HIGH_CONFIDENCE_BODY:
                    score += 2
                    strong = True
                else:
                    score += 1
        _scan(body, INTRO_PATTERNS, "intro")
        _scan(body, INVESTOR_PATTERNS, "investor", is_strong=True)
        _scan(body, SAVINGS_PATTERNS, "savings", is_strong=True)
        _scan(body, UNSUB_INDIVIDUAL_PATTERNS, "unsub", is_strong=True)
        _scan(body, SALES_TRACKING_PATTERNS, "tracking", weight=2, is_strong=True)

    if sender_email:
        _scan(sender_email, SALES_TRACKING_PATTERNS, "tracking-from", weight=2, is_strong=True)
        _scan(sender_email, DOMAIN_PATTERNS, "domain")

    is_cold = score >= COLD_OUTBOUND_THRESHOLD
    return ColdOutboundVerdict(
        is_cold=is_cold,
        score=score,
        hits=tuple(hits),
        confident=is_cold and strong,
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
