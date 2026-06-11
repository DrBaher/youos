"""Verify-before-accept: cheap deterministic checks on a generated draft.

A draft that *reads* well can still be unsafe to act on: written in the wrong
language, or stating a concrete email / link / number the model invented (it
appears nowhere in the inbound or thread). For an autonomous agent those are
the dangerous failures — a fluent reply that quotes a made-up address.

These checks are deterministic and run at ~zero cost. They split into:

* **blocking** — language mismatch, invented email address, invented link. Almost
  never legitimate in a reply; an action should be held for human review.
* **warnings** — an amount or a time/date not found in the inbound. Often
  legitimate (proposing a meeting slot, restating a known price), so surfaced
  but not blocking.

Failure-isolated by the caller; verification never blocks *drafting*, only the
decision to *act* on a draft (it collapses the draft's quality score, which the
auto-push quality floor already gates on).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ``inbound``/``thread_history`` are attacker-controlled. The unbounded ``+`` on
# the local part backtracks O(n^2) over a long no-``@`` run (50 KB ≈ 2 s, ~1 MB ≈
# 13 min — it stalls the unattended sweep and pins the /draft worker). Bounding the
# local part to its RFC-5321 max (64) makes each start position fail in ≤64 chars,
# so matching is linear; ``_MAX_VERIFY_CHARS`` (below) caps total work as well.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"https?://[^\s<>()\[\]]+", re.IGNORECASE)
_MONEY_RE = re.compile(
    r"[$€£]\s?\d[\d,.]*|\b\d[\d,.]*\s?(?:USD|EUR|GBP|dollars?|euros?|pounds?)\b",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s?(?:am|pm)?\b|\b\d{1,2}\s?(?:am|pm)\b", re.IGNORECASE)

# Un-grounded "concrete claim" phrases (b179). A fluent draft sometimes asserts
# a completed deliverable or an attachment that was never mentioned in the
# inbound — e.g. "Logo finalised. Pitch draft ready." invented out of nothing.
# These are caught as WARNINGS (not blocking): the human still reviews, and the
# phrasing is occasionally legitimate (the user really did attach something or
# finish a task), so we surface rather than hold. To stay low-false-positive,
# each entry is (claim_key, pattern): a match is flagged only when ``claim_key``
# is ABSENT from the grounding corpus (inbound + thread). So a reply that says
# "attached" in response to an inbound that itself mentions "attached" / the
# attachment topic is not flagged, while a fabricated "Logo finalised." with no
# prior mention is. Bounded literals only — no catastrophic backtracking.
_CLAIM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("attach", re.compile(r"\b(?:I(?:'ve| have)\s+)?attach(?:ed|ing)\b", re.IGNORECASE)),
    ("enclos", re.compile(r"\benclos(?:ed|ing)\b", re.IGNORECASE)),
    ("finalis", re.compile(r"\bfinali[sz]ed\b", re.IGNORECASE)),
    ("ready", re.compile(r"\b(?:is|are|now)\s+ready\b|\b\w+\s+ready\b", re.IGNORECASE)),
    ("complete", re.compile(r"\b(?:is|are|now)\s+completed?\b|\bcompleted\b", re.IGNORECASE)),
    ("signed", re.compile(r"\b(?:is|are|now)\s+signed\b|\bcountersigned\b", re.IGNORECASE)),
    ("confirmed", re.compile(r"\b(?:is|are|now)\s+confirmed\b", re.IGNORECASE)),
)

# Status assertions (b229). Live no_send review (2026-06-11) found the worst
# draft failure mode is asserting a COMPLETED state of the world — "June
# payment has been received", "Resignation filed with ADGM", "Direct debit is
# now active", "No further action needed" — when the agent did nothing and the
# thread says otherwise (one inbound was literally chasing the payment the
# draft claimed was received). Unlike the softer _CLAIM_PATTERNS above these
# are collected separately (``status_claims``): an AUTONOMOUS draft asserting
# an ungrounded completed state collapses quality (→ b188 abstain → the email
# is surfaced for review instead of queued with a fabricated draft).
# Interactive /draft calls and the deterministic eval path are unaffected.
# Same grounding rule: flagged only when ``claim_key`` is absent from the
# inbound + thread, so echoing the sender's own "no further action needed" or
# "has been filed" is never flagged. Bounded literals — no backtracking.
_STATUS_CLAIM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("received", re.compile(r"\b(?:has|have|had)\s+been\s+received\b|\b(?:was|were)\s+received\b", re.IGNORECASE)),
    ("filed", re.compile(r"\b(?:has|have)\s+been\s+filed\b|\b(?:was|were)\s+filed\b|\bfiled\s+with\s+(?:the\s+)?[A-Z]", re.IGNORECASE)),
    ("paid", re.compile(r"\b(?:has|have)\s+been\s+paid\b|\b(?:was|were)\s+paid\b", re.IGNORECASE)),
    ("processed", re.compile(r"\b(?:has|have)\s+been\s+processed\b|\b(?:was|were)\s+processed\b", re.IGNORECASE)),
    ("sent", re.compile(r"\b(?:has|have)\s+been\s+sent\b|\b(?:was|were)\s+sent\s+(?:to|out)\b", re.IGNORECASE)),
    ("updated", re.compile(r"\b(?:has|have)\s+been\s+updated\b", re.IGNORECASE)),
    ("resolved", re.compile(r"\b(?:is|are|has\s+been|have\s+been)\s+(?:now\s+)?resolved\b", re.IGNORECASE)),
    ("active", re.compile(r"\b(?:is|are)\s+now\s+(?:active|live|enabled|in\s+place|set\s+up)\b", re.IGNORECASE)),
    ("no further action", re.compile(r"\bno\s+(?:further\s+)?action\s+(?:is\s+)?(?:needed|required)\b", re.IGNORECASE)),
)

# Hard cap on the text any of the above regexes scan. The inbound + thread
# history are attacker-controlled and otherwise uncapped on this path (the
# 4000-char prompt cap protects generation, not verify_draft). 20 KB is far
# more than a real reply needs for grounding.
_MAX_VERIFY_CHARS = 20_000


@dataclass
class VerifyResult:
    ok: bool                                  # False if any blocking issue
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Ungrounded completed-state assertions (b229). Warning-severity for
    # interactive drafting, but the autonomous path collapses quality on them —
    # see the caller in app.generation.service.
    status_claims: list[str] = field(default_factory=list)

    @property
    def issues(self) -> list[str]:
        return [f"[block] {x}" for x in self.blocking] + [f"[warn] {x}" for x in self.warnings]


def _strip_url(u: str) -> str:
    return u.lower().rstrip(".,);:!?'\"")


def verify_draft(
    draft: str,
    *,
    inbound: str,
    thread_history: list[dict[str, str]] | None = None,
    account_email: str | None = None,
    sender: str | None = None,
    expected_language: str | None = None,
) -> VerifyResult:
    """Run the deterministic checks. ``inbound`` + ``thread_history`` are the
    grounding corpus; ``account_email`` / ``sender`` are allowed email
    addresses (the participants) even if not quoted in the body."""
    from app.core.text_utils import strip_signature

    # Cap every attacker-controlled input before any regex/lang-detect runs.
    d = (draft or "")[:_MAX_VERIFY_CHARS]
    inbound = (inbound or "")[:_MAX_VERIFY_CHARS]
    d_core = strip_signature(d)
    hay = inbound
    if thread_history:
        hay += "\n" + "\n".join((h.get("text") or "") for h in thread_history)
    hay = hay[:_MAX_VERIFY_CHARS]
    hay_l = hay.lower()

    blocking: list[str] = []
    warnings: list[str] = []

    # 1) Language match — the draft should answer in the inbound's language,
    # unless the caller knows better (b237: a per-sender reply-language habit
    # can legitimately override the inbound's language; verify against the
    # INTENDED language then, or the check would block exactly the behaviour
    # the profile asked for).
    if d_core.strip() and (inbound or "").strip():
        from app.core.text_utils import detect_language

        lang_in = expected_language or detect_language(inbound)
        lang_out = detect_language(d_core)
        if lang_in != lang_out:
            blocking.append(f"language mismatch (expected={lang_in}, draft={lang_out})")

    # 2) Invented email addresses — anything in the draft that isn't a
    # participant and wasn't in the inbound/thread.
    allowed: set[str] = {m.lower() for m in _EMAIL_RE.findall(hay)}
    if account_email:
        allowed.add(account_email.strip().lower())
    if sender:
        from app.core.sender import extract_email

        se = extract_email(sender)
        if se:
            allowed.add(se.strip().lower())
    for m in _EMAIL_RE.findall(d):
        if m.lower() not in allowed:
            blocking.append(f"invented email address: {m}")

    # 3) Invented links.
    for m in _URL_RE.findall(d):
        if _strip_url(m) not in hay_l:
            blocking.append(f"invented link: {m}")

    # 4) Amounts / times not found in the inbound — warn (often legitimate).
    for m in _MONEY_RE.findall(d):
        if m.strip().lower() not in hay_l:
            warnings.append(f"amount not in the inbound: {m.strip()}")
    for m in _TIME_RE.findall(d):
        if m.strip().lower() not in hay_l:
            warnings.append(f"time/date not in the inbound: {m.strip()}")

    # 5) Un-grounded concrete claims (b179) — the draft asserts an attachment or
    # a finished/confirmed deliverable whose claim word never appears in the
    # inbound or thread. Warn (the human reviews; the phrasing is sometimes
    # legitimate). Scan d_core so a boilerplate signature can't trip it; de-dup
    # so one repeated phrase warns once.
    seen_claims: set[str] = set()
    for key, pat in _CLAIM_PATTERNS:
        if key in hay_l:
            continue  # the claim is grounded in the inbound/thread — not invented
        m = pat.search(d_core)
        if m:
            phrase = m.group(0).strip().lower()
            if phrase not in seen_claims:
                seen_claims.add(phrase)
                warnings.append(f"unsupported claim not in the inbound: {m.group(0).strip()}")

    # 6) Ungrounded status assertions (b229) — the draft states a completed
    # state of the world ("has been received", "filed with X", "is now active",
    # "no further action needed") that appears nowhere in the inbound/thread.
    # Recorded separately so the autonomous path can act on them; also mirrored
    # into warnings so the review UI shows them.
    status_claims: list[str] = []
    for key, pat in _STATUS_CLAIM_PATTERNS:
        if key in hay_l:
            continue
        m = pat.search(d_core)
        if m:
            phrase = m.group(0).strip().lower()
            if phrase not in seen_claims:
                seen_claims.add(phrase)
                status_claims.append(m.group(0).strip())
                warnings.append(f"asserts unverified status: {m.group(0).strip()}")

    return VerifyResult(ok=not blocking, blocking=blocking, warnings=warnings, status_claims=status_claims)
