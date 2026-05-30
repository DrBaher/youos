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

    # 1) Language match — the draft should answer in the inbound's language.
    if d_core.strip() and (inbound or "").strip():
        from app.core.text_utils import detect_language

        lang_in = detect_language(inbound)
        lang_out = detect_language(d_core)
        if lang_in != lang_out:
            blocking.append(f"language mismatch (inbound={lang_in}, draft={lang_out})")

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

    return VerifyResult(ok=not blocking, blocking=blocking, warnings=warnings)
