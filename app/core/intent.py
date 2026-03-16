"""Keyword-based intent classification for inbound emails."""

from __future__ import annotations

import re

INTENTS: dict[str, list[str]] = {
    "meeting_request": ["meet", "meeting", "schedule", "calendar", "call", "sync", "catch up", "catchup", "availability", "slot"],
    "approval_needed": ["approve", "approval", "sign off", "sign-off", "authorize", "permission", "green light", "go-ahead"],
    "information_request": ["could you", "can you", "please send", "looking for", "need info", "wondering", "question", "what is", "how do"],
    "status_update": ["update", "status", "progress", "fyi", "heads up", "letting you know", "just to let you know"],
    "introduction": ["introducing", "introduction", "meet", "connect you", "putting you in touch", "i'd like you to meet"],
    "complaint": ["issue", "problem", "disappointed", "frustrated", "unacceptable", "complaint", "not working", "broken"],
    "thank_you": ["thank", "thanks", "appreciate", "grateful", "cheers for"],
    "proposal": ["proposal", "propose", "suggesting", "recommendation", "plan", "pitch", "idea"],
    "urgent": ["urgent", "asap", "immediately", "critical", "time-sensitive", "deadline", "eod", "end of day"],
    "general": [],
}

_INTENT_PATTERNS: dict[str, re.Pattern] = {}
for _intent, _keywords in INTENTS.items():
    if _keywords:
        escaped = [re.escape(kw) for kw in _keywords]
        _INTENT_PATTERNS[_intent] = re.compile(r"\b(?:" + "|".join(escaped) + r")\b", re.IGNORECASE)


def classify_intent(text: str) -> str:
    """Classify the intent of an inbound email text. Returns the intent key."""
    if not text:
        return "general"

    scores: dict[str, int] = {}
    for intent, pattern in _INTENT_PATTERNS.items():
        matches = pattern.findall(text)
        if matches:
            scores[intent] = len(matches)

    if not scores:
        return "general"

    return max(scores, key=scores.get)


def classify_intents_multi(text: str, *, max_intents: int = 3) -> list[str]:
    """Return ALL intents with >= 1 keyword match, sorted by score DESC, max 3.

    Returns ['general'] if none match.
    """
    if not text:
        return ["general"]

    scores: dict[str, int] = {}
    for intent, pattern in _INTENT_PATTERNS.items():
        matches = pattern.findall(text)
        if matches:
            scores[intent] = len(matches)

    if not scores:
        return ["general"]

    sorted_intents = sorted(scores, key=scores.get, reverse=True)
    return sorted_intents[:max_intents]
