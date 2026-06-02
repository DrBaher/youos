"""Cheap, local, deterministic grounding check for SUMMARIES (b191).

Digests (:mod:`app.agent.digest_tasks`) and per-thread catch-up summaries
(:mod:`app.agent.thread_summary`) are raw model output. Drafts get a verify
pass (:mod:`app.generation.verify`, incl. the b179 un-grounded-claim guard) but
summaries got none — a hallucinated date/amount/name in a summary read at a
glance is a real trust risk.

This module adds the missing check, mirroring the b179 draft-grounding
philosophy: catch *fabricated specifics*, allow honest summarization/paraphrase.

Design (deliberately constrained):

* **No model call, no network/egress.** Summaries are local-only; this stays
  local. Pure regex + string normalization. Deterministic.
* **Language-agnostic.** Source emails may be DE/FR/ES/EN. We match only on
  language-neutral *specifics* — numbers/amounts, dates/times, phone numbers,
  emails, URLs, multi-word Capitalized proper-noun tokens — NOT word overlap
  (which would falsely penalize a summary written in another language than the
  source, or a faithful paraphrase).
* **Flag invented specifics only.** A specific asserted in the SUMMARY that does
  not appear in (and isn't normalize-matchable to) the SOURCE is "ungrounded".
  Faithful abstraction/paraphrase introduces no new specifics, so it is never
  penalized. Conservative: only clear fabrications are flagged.

The reusable regex primitives (``_EMAIL_RE``, ``_URL_RE``, ``_MONEY_RE``,
``_TIME_RE``) are imported from :mod:`app.generation.verify` so the summary and
draft paths share one definition of "a concrete specific".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.generation.verify import _EMAIL_RE, _MONEY_RE, _TIME_RE, _URL_RE

# Same hard cap as verify: source/summary are bounded before any regex runs so a
# pathological input can't pin the (read-only) digest/summary path.
_MAX_GROUNDING_CHARS = 20_000

# A run of digits (with optional grouping/decimal separators) — the language-
# neutral signal that does most of the work. Captures "1,250.00", "1.250,00",
# "49", "2026". Time-of-day digit-runs are also caught by _TIME_RE; we keep
# this simple and normalize aggressively at match time (see _digit_signatures).
_NUMBER_RE = re.compile(r"\d[\d.,]*\d|\d")

# Phone numbers: an international/grouped run of digits with separators, long
# enough to not collide with a plain small integer. Language-neutral.
_PHONE_RE = re.compile(r"(?:\+\d[\d().\s-]{6,}\d)|(?:\b\d[\d().\s-]{7,}\d\b)")

# A multi-word Capitalized proper-noun span: two or more *adjacent* Capitalized
# tokens ("Acme Corp", "Jane Doe", "José Müller"). A SINGLE capitalized word is
# intentionally NOT flagged — sentence-initial capitalization and common nouns
# make single-token capitalization far too noisy (false positives). And we do
# NOT bridge a lowercase connector ("and"/"und"/"&"): "Jane and Bob" must NOT be
# read as one fabricated name when Jane and Bob both appear in the source — that
# was a real over-trigger. Requiring strictly adjacent capitalized tokens keeps
# this conservative (it under-flags multi-word place names like "Frankfurt am
# Main" rather than risk penalizing a faithful summary). Unicode-aware so
# DE/FR/ES accented names match (José Müller, Renée Dupont, Société Générale).
_PROPER_NOUN_RE = re.compile(
    r"[A-ZÀ-Þ][\wÀ-ÿ'’-]+(?:\s+[A-ZÀ-Þ][\wÀ-ÿ'’-]+)+"
)

# Conservative default: a summary is treated as ungrounded only when MORE than
# this many distinct high-risk specifics are fabricated. One stray token (an
# odd capitalization, a number that's really a paraphrase artifact) does not
# trip the gate — we want clear fabrication, not a hair-trigger. Callers may
# pass a stricter ``max_ungrounded`` (thread summaries use 0: a wrong catch-up
# is worse than none).
_DEFAULT_MAX_UNGROUNDED = 1


@dataclass
class GroundingResult:
    """Outcome of a summary grounding check.

    ``grounded`` is the conservative verdict the caller acts on. ``score`` is a
    [0,1] fraction of summary specifics supported by the source (1.0 when the
    summary asserts no checkable specifics — vacuously grounded). ``ungrounded``
    lists the fabricated specifics for telemetry/debugging.
    """

    grounded: bool
    score: float
    ungrounded: list[str] = field(default_factory=list)
    checked: int = 0  # how many specifics we extracted from the summary


def _norm_digits(s: str) -> str:
    """All digits of a token, in order — a separator/format-agnostic signature.

    Handles thousand separators and decimal-comma vs decimal-point variance:
    "1,250.00", "1.250,00" and "1250,00" all reduce to "125000". A summary that
    re-formats a source amount (or writes the same number with different
    grouping) still matches.
    """
    return re.sub(r"\D", "", s)


def _digit_signatures(text: str) -> set[str]:
    """The set of digit-only signatures of every number-bearing token in text —
    used as the normalized haystack for amounts/numbers/dates/times/phones."""
    sigs: set[str] = set()
    for rx in (_NUMBER_RE, _MONEY_RE, _TIME_RE, _PHONE_RE):
        for m in rx.findall(text):
            d = _norm_digits(m)
            if d:
                sigs.add(d)
    return sigs


def _matched_in_source(token: str, source_l: str, source_digits: set[str]) -> bool:
    """Is ``token`` (a specific extracted from the summary) supported by source?

    Two cheap checks: (1) verbatim case-insensitive substring of the source;
    (2) for number-bearing tokens, its digit-signature appears among the
    source's number signatures (handles separator/format variance and the case
    where the number is embedded in a larger source token)."""
    t = token.strip()
    if not t:
        return True
    if t.lower() in source_l:
        return True
    d = _norm_digits(t)
    if d:
        if d in source_digits:
            return True
        # A long, specific digit run that is a substring of a source number
        # (e.g. summary "0791234567" vs source "+41 79 123 45 67") counts as
        # grounded. Require length >= 4 so a short "250" doesn't match every
        # larger number and create false negatives in the *other* direction.
        if len(d) >= 4 and any(d in sig for sig in source_digits):
            return True
    return False


def check_summary_grounding(
    summary_text: str,
    source_text: str,
    *,
    max_ungrounded: int = _DEFAULT_MAX_UNGROUNDED,
) -> GroundingResult:
    """Check whether the specifics asserted in ``summary_text`` are supported by
    ``source_text``. Deterministic, local, no egress.

    Extracts high-risk specifics from the SUMMARY only — amounts/numbers,
    dates/times, phone/email/URL, multi-word Capitalized proper-noun spans — and
    verifies each is present (verbatim or via a normalized digit-signature
    match) in the source. Faithful abstraction introduces no new specifics, so a
    paraphrase that drops/compresses detail is never flagged. Language-agnostic:
    nothing here depends on word overlap, so a DE/FR/ES source and an EN summary
    (or vice versa) match on the shared numbers/dates/names.

    ``grounded`` is True unless the number of ungrounded specifics EXCEEDS
    ``max_ungrounded`` (conservative; default tolerates one). ``score`` is the
    supported-fraction (1.0 when the summary asserts no checkable specifics)."""
    summary = (summary_text or "")[:_MAX_GROUNDING_CHARS]
    source = (source_text or "")[:_MAX_GROUNDING_CHARS]
    if not summary.strip():
        return GroundingResult(grounded=True, score=1.0, ungrounded=[], checked=0)

    source_l = source.lower()
    source_digits = _digit_signatures(source)

    # Extract candidate specifics from the SUMMARY. We de-dup case-insensitively
    # so one repeated specific counts once.
    candidates: list[str] = []
    for rx in (_EMAIL_RE, _URL_RE, _MONEY_RE, _TIME_RE, _PHONE_RE, _PROPER_NOUN_RE, _NUMBER_RE):
        for m in rx.findall(summary):
            tok = m.strip() if isinstance(m, str) else str(m).strip()
            if tok:
                candidates.append(tok)

    seen: set[str] = set()
    ungrounded: list[str] = []
    checked = 0
    for tok in candidates:
        key = tok.lower()
        if key in seen:
            continue
        seen.add(key)
        checked += 1
        if not _matched_in_source(tok, source_l, source_digits):
            ungrounded.append(tok)

    if checked == 0:
        return GroundingResult(grounded=True, score=1.0, ungrounded=[], checked=0)

    score = 1.0 - (len(ungrounded) / checked)
    grounded = len(ungrounded) <= max_ungrounded
    return GroundingResult(grounded=grounded, score=score, ungrounded=ungrounded, checked=checked)
