"""Time-criticality scoring for inbound email.

Urgency was already *detected* in two places and then thrown away: the
``"urgent"`` intent label in :mod:`app.core.intent` and the high-stakes verdict
from :func:`app.agent.escalation.assess_stakes`. Neither fed the ordering of the
pending queue (which sorted on ``needs_reply_score`` alone) nor the digest's
"worth attention" line (a one-off subject-keyword heuristic). This module turns
those hollow signals into one transparent score the queue + digest can rank on.

``compute_urgency_score`` is pure and deterministic. It combines a small set of
signals into a clamped [0, 1] score and returns the human-readable *reasons* so
the user can see WHY something was flagged. It is MULTILINGUAL: deadline /
urgency markers are matched in EN / DE / FR / ES (mirroring the language work in
the drafting pipeline).

SAFETY: this score is for ORDERING + VISIBILITY only. It MUST NEVER be wired
into a send / auto-send / auto-push gate in a way that makes an outbound action
*more* likely. High urgency may only make the agent MORE conservative (e.g.
surface for a human), never less. The never-send invariant is unaffected.
"""

from __future__ import annotations

import re

# --- Multilingual urgency / deadline markers --------------------------------
#
# Two tiers, both word-boundaried and case-insensitive:
#   * STRONG  — explicit urgency words ("urgent", "asap", "dringend", "eilt",
#     "urgente"). A single hit is a strong time-critical signal.
#   * DEADLINE — date/time-bound markers ("deadline", "eod", "by friday",
#     "today", "tomorrow", "Frist", "bis", "heute", "morgen", "délai", "avant",
#     "aujourd'hui", "demain", "plazo", "antes", "hoy", "mañana"). A hit means
#     the sender named a time bound, which raises priority even without an
#     explicit "urgent".
#
# Kept deliberately small and documented so the weights stay legible. Accented
# variants (mañana / délai / aujourd'hui) are listed explicitly; the body is
# matched as-is (no transliteration) so they fire on real DE/FR/ES mail.

_STRONG_URGENT = [
    # EN
    "urgent", "asap", "immediately", "right away", "time-sensitive",
    "time sensitive", "critical",
    # DE
    "dringend", "eilt", "sofort", "umgehend",
    # FR
    "urgent", "urgente", "immédiatement", "au plus vite",
    # ES
    "urgente", "inmediatamente", "cuanto antes",
]

_DEADLINE = [
    # EN
    "deadline", "eod", "end of day", "due", "overdue", "by today",
    "by tomorrow", "by end of day", "today", "tomorrow", "no later than",
    # DE
    "frist", "fällig", "heute", "morgen", "bis heute", "bis morgen",
    "spätestens",
    # FR
    "délai", "echéance", "échéance", "avant", "aujourd'hui", "aujourd hui",
    "demain", "au plus tard",
    # ES
    "plazo", "vencimiento", "antes", "hoy", "mañana", "manana", "fecha límite",
    "fecha limite",
]


def _compile(words: list[str]) -> re.Pattern:
    # Dedup while preserving order; longest-first so "end of day" wins over "due".
    seen: set[str] = set()
    uniq: list[str] = []
    for w in sorted(words, key=len, reverse=True):
        lw = w.lower()
        if lw not in seen:
            seen.add(lw)
            uniq.append(w)
    escaped = [re.escape(w) for w in uniq]
    return re.compile(r"(?<!\w)(?:" + "|".join(escaped) + r")(?!\w)", re.IGNORECASE)


_STRONG_PAT = _compile(_STRONG_URGENT)
_DEADLINE_PAT = _compile(_DEADLINE)

# Explicit "by <weekday>" / "by <Mon DD>" date references — a concrete time
# bound the keyword lists don't cover. EN + the most common day/month names.
_BY_DATE_PAT = re.compile(
    r"\bby\s+(?:"
    r"mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:rs|rsday)?|"
    r"fri(?:day)?|sat(?:urday)?|sun(?:day)?|"
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|"
    r"\d{1,2}(?:st|nd|rd|th)?"
    r")\b",
    re.IGNORECASE,
)

# --- Weights (documented, sane, additive then clamped) ----------------------
#
# Chosen so that any single strong signal lifts an item clearly above a routine
# one, two signals saturate near the top, and no single signal alone pins to
# 1.0 (room for combination). All additive, final clamp to [0, 1].
_W_URGENT_INTENT = 0.45    # the "urgent" intent label fired (classify_intents_multi)
_W_STRONG_MARKER = 0.40    # an explicit urgency word in subject/body
_W_DEADLINE = 0.30         # a deadline / time-bound marker
_W_BY_DATE = 0.20          # an explicit "by <date>" reference
_W_HIGH_STAKES = 0.20      # assess_stakes() == 'high' (money/legal/commitment)
_W_END_QUESTION = 0.10     # the new content ends with a question


def compute_urgency_score(
    *,
    subject: str | None,
    body: str | None,
    intents: list[str] | None = None,
    stakes: str | None = None,
) -> tuple[float, list[str]]:
    """Score how time-critical an inbound message is, in ``[0.0, 1.0]``.

    Combines (all optional, all additive then clamped):

    * ``"urgent"`` in ``intents`` (the intent label from
      :func:`app.core.intent.classify_intents_multi`) — strong.
    * explicit multilingual urgency markers (EN/DE/FR/ES) in subject or body.
    * deadline / time-bound markers (EN/DE/FR/ES) + ``by <date>`` references.
    * ``stakes == 'high'`` (from :func:`app.agent.escalation.assess_stakes`).
    * the new content ending in a question (re-uses the needs-reply signal).

    ``intents`` / ``stakes`` are accepted pre-computed so callers that already
    ran the classifier / stakes assessment don't pay for it twice; when omitted
    they are computed here. Returns ``(score, reasons)`` — ``reasons`` is a list
    of short human-readable strings explaining the score, for transparency in
    the UI / digest.

    Pure, deterministic, side-effect free. ORDERING + VISIBILITY only — see the
    module docstring's safety note.
    """
    reasons: list[str] = []
    score = 0.0

    text = f"{subject or ''}\n{body or ''}"

    # 1) "urgent" intent label (computed-then-discarded today).
    if intents is None:
        try:
            from app.core.intent import classify_intents_multi

            intents = classify_intents_multi(body or subject or "")
        except Exception:
            intents = []
    if intents and "urgent" in intents:
        score += _W_URGENT_INTENT
        reasons.append("'urgent' intent detected")

    # 2) Explicit urgency markers (multilingual).
    if _STRONG_PAT.search(text):
        score += _W_STRONG_MARKER
        reasons.append("urgency marker (urgent/asap/dringend/urgente/…)")

    # 3) Deadline / time-bound markers (multilingual) + "by <date>".
    deadline_hit = bool(_DEADLINE_PAT.search(text))
    by_date_hit = bool(_BY_DATE_PAT.search(text))
    if deadline_hit:
        score += _W_DEADLINE
        reasons.append("deadline / time-sensitivity marker")
    if by_date_hit:
        score += _W_BY_DATE
        reasons.append("explicit 'by <date>' reference")

    # 4) High stakes (money / legal / firm commitment). assess_stakes already
    # gates auto-push; here it only lifts visibility.
    if stakes is None:
        try:
            from app.agent.escalation import assess_stakes

            stakes = assess_stakes(subject, body)
        except Exception:
            stakes = "low"
    if stakes == "high":
        score += _W_HIGH_STAKES
        reasons.append("high-stakes content (money/legal/commitment)")

    # 5) Question at the END of the new content — "they're waiting on an
    # answer". Re-uses needs_reply's trimming so quoted history doesn't count.
    end_q = False
    try:
        from app.core.text_utils import extract_new_content, strip_signature

        scoring_text = strip_signature(extract_new_content(body or ""))
        if not scoring_text.strip():
            scoring_text = body or ""
    except Exception:
        scoring_text = body or ""
    if "?" in scoring_text[-200:]:
        end_q = True
    if end_q:
        score += _W_END_QUESTION
        reasons.append("ends with a question")

    score = max(0.0, min(1.0, score))
    return score, reasons
