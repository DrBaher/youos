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
    # b290: fabricated confirmations. Live review found the model asserting a
    # third party (or itself) "confirmed" something the thread never states —
    # "Johannes has confirmed the call will be on 5th July" (#46536), "Confirmed —
    # all parties have signed the NDA" (#23327) on a thread that was only a
    # scheduling poll. Grounded on the stem "confirm", so echoing the sender's own
    # "please confirm" does still ground it (accepted: a request grounds the word).
    ("confirm", re.compile(
        r"\b(?:has|have)\s+confirmed\b|\bconfirms\b"
        r"|(?:^|[.!?]\s+|,\s+)confirmed[\s:—,-]", re.IGNORECASE)),
    # Fabricated "signed" as a completed state — "all parties have signed",
    # "the NDA has been signed". _CLAIM_PATTERNS carries a softer (warning-only)
    # "signed"; this collapses on the autonomous path when un-grounded.
    ("sign", re.compile(
        r"\b(?:have|has)\s+(?:been\s+)?signed\b|\b(?:is|are|now)\s+signed\b"
        r"|\bcountersigned\b", re.IGNORECASE)),
    # Fabricated money movement — "$560 transferred to MED Bank" (#14391) on an
    # inbound that merely *requested* the transfer. Keyed on "transferred" (not
    # "transfer"), so a request ("please transfer") never grounds the completion.
    ("transferred", re.compile(
        r"\b(?:has|have|had)\s+been\s+transferred\b|\b(?:was|were)\s+transferred\b"
        r"|\btransferred\s+to\b", re.IGNORECASE)),
    # Fabricated document location/existence — "the contract is already in your
    # per-debtor folder under Intel Lab LLC" (#43843): invents where a file lives
    # in response to a request to share it. Keyed on "folder".
    ("folder", re.compile(
        r"\b(?:is|are)\s+(?:already\s+)?(?:in|available\s+in|saved\s+in|stored\s+in|filed\s+in)\s+"
        r"(?:your|the|our)\b.{0,40}\bfolder\b"
        r"|\balready\s+in\s+(?:your|the|our)\b.{0,40}\bfolder\b", re.IGNORECASE)),
)

# German completion assertions (b286). The English patterns above never fire
# on the ~27% German half of the queue: a live review found "beide Konten sind
# nun abgedeckt" (a false "the accounts are now covered" to the bank) and
# "abgeschlossen" slipping through. Same grounding rule — flagged only when the
# participle is ABSENT from the inbound/thread, so acknowledging a completion
# the OTHER party stated ("danke, überwiesen") is not flagged. The key IS the
# participle, so grounding fires only on the exact same completion word.
_STATUS_CLAIM_PATTERNS_DE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("abgedeckt", re.compile(r"\babgedeckt\b", re.IGNORECASE)),
    ("erledigt", re.compile(r"\berledigt\b", re.IGNORECASE)),
    ("überwiesen", re.compile(r"\b[uü]berwiesen\b", re.IGNORECASE)),
    ("bezahlt", re.compile(r"\bbezahlt\b", re.IGNORECASE)),
    ("eingereicht", re.compile(r"\beingereicht\b", re.IGNORECASE)),
    ("abgeschlossen", re.compile(r"\babgeschlossen\b", re.IGNORECASE)),
    ("bestätigt", re.compile(r"\best[äa]tigt\b", re.IGNORECASE)),
    ("versendet", re.compile(r"\b(?:versendet|verschickt|abgeschickt|versandt)\b", re.IGNORECASE)),
    ("durchgeführt", re.compile(r"\bdurchgef[üu]hrt\b", re.IGNORECASE)),
    ("umgesetzt", re.compile(r"\bumgesetzt\b", re.IGNORECASE)),
    ("freigegeben", re.compile(r"\bfreigegeben\b", re.IGNORECASE)),
    ("eingerichtet", re.compile(r"\beingerichtet\b", re.IGNORECASE)),
    ("aktiviert", re.compile(r"\baktiviert\b|\b(?:ist|sind)\s+(?:jetzt|nun)\s+aktiv\b", re.IGNORECASE)),
)

# Invented deadline / over-commitment (b286, extends b277). The model freely
# promises firm deadlines the user never made — "by EOD", "by EOD tomorrow" —
# on Baher's behalf. Flagged only when the deadline token is NOT in the inbound
# (the sender didn't ask for it), so a genuine "Friday works" reply to a
# "can you do Friday?" request is left alone. Vague windows ("this/next week")
# are excluded to keep false positives low.
_COMMITMENT_RE = re.compile(
    r"\bby\s+(?P<en>eod|cob|end\s+of\s+(?:the\s+)?day|end\s+of\s+business|"
    r"tomorrow|tonight|noon|"
    r"mon(?:day)?|tues?(?:day)?|wed(?:nesday)?|thu(?:rs)?(?:day)?|"
    r"fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b"
    r"|\bbis\s+(?P<de>eod|morgen|heute\s+abend|freitag|montag|dienstag|"
    r"mittwoch|donnerstag)\b",
    re.IGNORECASE,
)

# Bare future-time over-commitment (b297). The 4B LoRA promises a near-term
# action the sender never requested a time for — "I'll follow up with Fyoldi
# tomorrow", "I'll resend them today". Distinct from _COMMITMENT_RE (which needs
# an explicit "by <deadline>"): here the tell is a first-person future intent
# followed within a clause by a concrete near-term time token. Flagged only when
# that time token is NOT in the grounding corpus (the sender didn't set it), so
# a "Friday works?" → "yes, Friday" reply stays clean.
_OVERCOMMIT_RE = re.compile(
    r"\bI(?:['’]ll| will| shall| am going to|['’]m going to|['’]ll aim to| will aim to)\b"
    r"[^.?!\n]{0,70}?\b(?P<t>today|tonight|tomorrow|this\s+(?:morning|afternoon|evening)|"
    r"heute|morgen|heute\s+abend)\b",
    re.IGNORECASE,
)

# Ungrounded first-person completed EXTERNAL action (b297). The model asserts it
# performed a real-world action it categorically cannot have — "I've reviewed the
# GLS delivery log", "I've spoken with the notary" — on a thread that never
# mentions the action. Flagged only when the action stem is absent from the
# grounding corpus, so a genuine "I've reviewed the agreement" on an agreement
# thread (where "review" is quoted) is left alone.
_DID_ACTION_RE = re.compile(
    r"\bI(?:['’]ve| have)\s+(?:already\s+|just\s+)?"
    r"(?P<v>review|check|look(?:ed)?\s+(?:at|into|through)|"
    r"gone through|spoke|spoken|contact|call|reach|verif)",
    re.IGNORECASE,
)
# Grounding stems for _DID_ACTION_RE, keyed by the captured verb fragment.
_ACTION_STEMS = ("review", "check", "look", "spoke", "spoken", "speak", "spok",
                 "contact", "call", "reach", "verif")

# Leaked internal scaffolding / placeholders (b286). A live draft (2026-07)
# ended with the model's own prompt block copied verbatim — "[FACTS CONTEXT]
# About you: Based in Dubai, …" — and another surfaced a "[list attached]"
# placeholder as if it were text. These strings NEVER belong in a real reply,
# so they are BLOCKING (held for review) regardless of the autonomous/eval
# path. Deterministic post-processing also strips the [FACTS CONTEXT] block in
# app.generation.service._strip_scaffolding; this is the belt-and-suspenders
# catch for anything the strip misses (inline placeholders, partial markers).
_PLACEHOLDER_RE = re.compile(
    r"\[\s*FACTS\s+CONTEXT"
    r"|\[\s*(?:list attached|insert[^\]]*|your name|your \w+ here|placeholder"
    r"|link|url|date|todo|tbd|xx+)\s*\]"
    r"|\{\s*name\s*\}",
    re.IGNORECASE,
)

# Personal-life fabrication (b286). The 4B LoRA over-learned Baher's warm,
# family-referencing voice and now INVENTS family details on unrelated threads
# — "your new baby's arrival is a lot of energy!" to a first-time contact, "our
# daughter's 4th birthday was last week" on a cold AWS pitch, "Kinder sind
# gesund" appended to a formal tax reply. Flagged only when NO family/personal
# stem appears anywhere in the grounding corpus (inbound + thread): if the
# sender really did mention a baby/wedding/etc., a warm acknowledgement is
# correct and not flagged. Collapses quality on the autonomous path (→ abstain
# → surfaced for review), like status claims.
_PERSONAL_LIFE_RE = re.compile(
    r"\b(?:new[\s-]?born|new baby|the baby|your baby|a baby|baby'?s|"
    r"pregnan\w+|maternity|paternity|honeymoon|"
    r"nap[\s-]?time|"
    r"(?:your|our|my|the)\s+(?:kids|children|son|daughter|wife|husband|family)|"
    r"\d+(?:st|nd|rd|th)?\s+birthday|"
    # German
    r"kinder\s+sind\s+gesund|neugeboren|schwanger\w*|geburtstag)\b",
    re.IGNORECASE,
)
# Family/personal terms that, if present in the grounding corpus, mean the
# draft's personal reference is grounded (the thread raised it first). Matched
# on WORD BOUNDARIES — a substring test wrongly grounds on "son" inside
# "reason"/"person" (and "nap" inside "snap"), suppressing real fabrications.
_FAMILY_GROUND_RE = re.compile(
    r"\b(?:bab(?:y|ies)|new[\s-]?born|neugeboren|birthday|geburtstag|"
    r"wedding|hochzeit|honeymoon|pregnan\w*|schwanger\w*|maternity|paternity|"
    r"nap|kids?|child(?:ren)?|kinder|daughter|sons?|wife|husband|"
    r"family|familie)\b",
    re.IGNORECASE,
)

# Hallucinated "review meeting" artifact (b286). The model repeatedly invents
# a nonexistent "tomorrow's full review meeting" / "in der nächsten
# Review-Mitteilung" — the internal batch/"review" framing leaking into the
# email body. Flagged when the phrase is not grounded in the thread.
_REVIEW_MEETING_RE = re.compile(
    r"\b(?:full |the )?review meeting\b"
    r"|\breview[-\s]mitteilung\b"
    r"|\bin (?:tomorrow'?s|the next|our next) (?:full )?review\b"
    r"|\bn[äa]chste[nrs]?\s+review[-\s]?(?:meeting|mitteilung|besprechung)\b",
    re.IGNORECASE,
)

# Sign-off tokens that mark the closing region of a reply (EN + DE). Used with
# the sender's name to detect a draft SIGNED AS THE SENDER (speaker inversion).
# NB: no trailing \b on the German prefixes — "viele gr" must still match
# inside "viele grüße" / "viele gruesse", where "gr" is not a word boundary.
_SIGNOFF_RE = re.compile(
    r"regards|best|thanks|thank you|cheers|sincerely|yours|"
    r"gr[üu](?:ss|ß|ess)e|mit freundlichen|"
    r"(?:viele|liebe|beste|herzliche|freundliche)\s+gr|danke",
    re.IGNORECASE,
)
# Opening salutation words (EN + DE) — a first line starting with one of these
# and containing the USER's own name means the draft greets the user, i.e. it
# was written from the sender's perspective.
_GREETING_OPEN_RE = re.compile(
    r"^\s*(?:hi|hey|hello|dear|hallo|liebe[rs]?|sehr\s+geehrte)\b",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[a-zà-öø-ÿ]+", re.IGNORECASE)

# Smart/curly apostrophes → straight quote, so apostrophe-bearing patterns
# ("baby'?s") match model output that uses U+2019 (b290).
_APOS_TABLE = {ord(c): "'" for c in "‘’ʼ′´`"}

# Hard cap on the text any of the above regexes scan. The inbound + thread
# history are attacker-controlled and otherwise uncapped on this path (the
# 4000-char prompt cap protects generation, not verify_draft). 20 KB is far
# more than a real reply needs for grounding.
_MAX_VERIFY_CHARS = 20_000


def _name_tokens(display: str | None) -> list[str]:
    """Alphabetic name tokens (len ≥ 3) from a From display name, dropping the
    email part and handling both "First Last" and "Last, First"."""
    if not display:
        return []
    s = re.sub(r"<[^>]*>", " ", display).replace('"', " ").replace("'", " ")
    toks: list[str] = []
    for p in re.split(r"[,\s]+", s):
        if len(p) >= 3 and re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ.'-]+", p):
            toks.append(p.lower())
    return toks


@dataclass
class VerifyResult:
    ok: bool                                  # False if any blocking issue
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Ungrounded completed-state assertions (b229). Warning-severity for
    # interactive drafting, but the autonomous path collapses quality on them —
    # see the caller in app.generation.service.
    status_claims: list[str] = field(default_factory=list)
    # Ungrounded fabrications (b286): invented family/personal details,
    # speaker inversion (draft addressed to / signed as the wrong person), and
    # the hallucinated "review meeting" artifact. Treated like status_claims by
    # the caller — collapses quality on the autonomous path (→ abstain →
    # surfaced for review), never touched on the deterministic eval path.
    fabrications: list[str] = field(default_factory=list)

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
    user_name: str | None = None,
) -> VerifyResult:
    """Run the deterministic checks. ``inbound`` + ``thread_history`` are the
    grounding corpus; ``account_email`` / ``sender`` are allowed email
    addresses (the participants) even if not quoted in the body. ``user_name``
    is the account owner's display name — used to detect speaker inversion (a
    draft addressed to, or signed as, the wrong party)."""
    from app.core.text_utils import strip_signature

    # Cap every attacker-controlled input before any regex/lang-detect runs.
    d = (draft or "")[:_MAX_VERIFY_CHARS]
    inbound = (inbound or "")[:_MAX_VERIFY_CHARS]
    d_core = strip_signature(d)
    # b290: strip_signature over-strips when the BODY opens with a sign-off-like
    # phrase — a greeting-stutter draft "Hi Franz,\n\nThanks Franz,\nJohannes has
    # confirmed …" collapses to just "Hi Franz,", so every content/fabrication
    # scan below (which runs on the de-signatured text) sees nothing and misses
    # the fabrication (#46536). When the strip removed most of the draft, it
    # almost certainly ate real body — fall back to the raw draft for the content
    # scans. The email/link/language checks keep using d_core as before.
    _d_stripped = d.strip()
    d_body = d_core if len(d_core.strip()) >= max(40, 0.5 * len(_d_stripped)) else _d_stripped
    # b290: normalise curly/smart apostrophes to a straight quote before the
    # content scans. The 4B LoRA emits "baby's" / "daughter's" with a curly ’
    # (U+2019), which defeats every apostrophe-bearing pattern (baby'?s,
    # daughter's) and let a fabricated-family draft through (#52452). Applied to
    # both the draft body and the grounding corpus so matching stays symmetric.
    d_body = d_body.translate(_APOS_TABLE)
    hay = inbound
    if thread_history:
        hay += "\n" + "\n".join((h.get("text") or "") for h in thread_history)
    hay = hay[:_MAX_VERIFY_CHARS].translate(_APOS_TABLE)
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
        m = pat.search(d_body)
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
    for key, pat in (*_STATUS_CLAIM_PATTERNS, *_STATUS_CLAIM_PATTERNS_DE):
        if key in hay_l:
            continue
        m = pat.search(d_body)
        if m:
            phrase = m.group(0).strip().lower()
            if phrase not in seen_claims:
                seen_claims.add(phrase)
                status_claims.append(m.group(0).strip())
                warnings.append(f"asserts unverified status: {m.group(0).strip()}")

    # 7) Leaked scaffolding / placeholders — BLOCKING. These never belong in a
    # real reply (see _PLACEHOLDER_RE). Scan the whole draft, not just d_core.
    for m in _PLACEHOLDER_RE.finditer(d):
        blocking.append(f"leaked scaffolding/placeholder: {m.group(0).strip()}")

    # 8) Ungrounded fabrications — collapse on the autonomous path (like status
    # claims). Family/personal detail invented with no family stem anywhere in
    # the grounding corpus; the hallucinated "review meeting"; and speaker
    # inversion (draft addressed to / signed as the wrong party).
    fabrications: list[str] = []
    family_grounded = bool(_FAMILY_GROUND_RE.search(hay_l))
    if not family_grounded:
        pm = _PERSONAL_LIFE_RE.search(d_body)
        if pm:
            fabrications.append(f"invented personal/family detail: {pm.group(0).strip()}")
    if "review meeting" not in hay_l and "review-mitteilung" not in hay_l:
        rm = _REVIEW_MEETING_RE.search(d_body)
        if rm:
            fabrications.append(f"hallucinated review meeting: {rm.group(0).strip()}")

    # Invented deadline / over-commitment — flagged only when the deadline token
    # is not in the grounding corpus (the sender didn't ask for it).
    for cm in _COMMITMENT_RE.finditer(d_body):
        token = (cm.group("en") or cm.group("de") or "").strip().lower()
        if token and token not in hay_l:
            fabrications.append(f"invented deadline: {cm.group(0).strip()}")
            break

    # b297: bare future-time over-commitment ("I'll follow up … tomorrow") whose
    # time token the sender never set.
    for om in _OVERCOMMIT_RE.finditer(d_body):
        token = re.sub(r"\s+", " ", om.group("t")).strip().lower()
        if token and token not in hay_l:
            fabrications.append(f"invented commitment: {om.group(0).strip()}")
            break

    # b297: first-person completed external action absent from the grounding
    # corpus ("I've reviewed the GLS delivery log" on a thread that never
    # mentions reviewing anything).
    am = _DID_ACTION_RE.search(d_body)
    if am and not any(stem in hay_l for stem in _ACTION_STEMS):
        fabrications.append(f"invented completed action: {am.group(0).strip()}")

    # Speaker inversion. Work on the RAW draft (not d_core): the signed-as-
    # sender tell IS a trailing signature, which strip_signature would remove.
    _lines = [ln for ln in d.strip().splitlines() if ln.strip()]
    if _lines:
        user_toks = {t for t in _name_tokens(user_name)}
        # (a) addressed to the user — the draft was written from the sender's
        # perspective. Two shapes, both requiring DIRECT ADDRESS (not a mere
        # mention: "my name is Baher" in a self-intro must NOT trip this):
        #   (a1) greeting inversion — line 0 is a salutation naming the user
        #        ("Hi Baher," / "Sehr geehrter Herr …").
        #   (a2) vocative inversion — the user's name set off by punctuation as a
        #        form of address ("Thanks for the message, Baher.") on any line
        #        except the last. b290: the last line is the sign-off region —
        #        "Best, Baher" is the user's OWN correct sign-off, not inversion —
        #        so it is excluded. Catches #20468 (banker's-voice reply).
        first_words = set(_WORD_RE.findall(_lines[0].lower()))
        _greeting_inv = (
            user_toks and (user_toks & first_words)
            and _GREETING_OPEN_RE.search(_lines[0])
        )
        _vocative_inv = False
        if user_toks and not _greeting_inv:
            _voc_re = re.compile(
                r"(?:^|[,;(–—-])\s*(?:" + "|".join(re.escape(t) for t in user_toks) + r")\b\s*[.,!?;:]",
                re.IGNORECASE,
            )
            _last = len(_lines) - 1
            for i, ln in enumerate(_lines):
                if not _voc_re.search(ln):
                    continue
                # Skip a match on a SHORT trailing line — that's the user's own
                # sign-off ("Best, Baher."), not inversion. A long final line is
                # real body text (#20468's whole banker's-voice reply is one line
                # that ends the draft), so it still counts.
                if i == _last and len(_WORD_RE.findall(ln)) <= 6:
                    continue
                _vocative_inv = True
                break
        if _greeting_inv or _vocative_inv:
            fabrications.append("addressed to the user (speaker inversion)")
        elif len(_lines) >= 3:
            # (b) signed as the sender: a distinct TRAILING signature line names
            # the sender (not the user), with a sign-off on it or the line above
            # — the draft signs off as the person it should be replying to. The
            # ≥3-line gate keeps the greeting out of the signature region, and
            # the short-last-line + sign-off requirements avoid firing on a
            # normal closing sentence that merely addresses the recipient.
            last = _lines[-1].strip()
            prev = _lines[-2].strip()
            last_words = set(_WORD_RE.findall(last.lower()))
            sender_toks = {t for t in _name_tokens(sender)}
            if (
                len(last_words) <= 6
                and (sender_toks & last_words)
                and not (user_toks & last_words)
                and (_SIGNOFF_RE.search(last.lower()) or _SIGNOFF_RE.search(prev.lower()))
            ):
                fabrications.append("signed as the sender (speaker inversion)")

    for f in fabrications:
        warnings.append(f)

    return VerifyResult(
        ok=not blocking,
        blocking=blocking,
        warnings=warnings,
        status_claims=status_claims,
        fabrications=fabrications,
    )
