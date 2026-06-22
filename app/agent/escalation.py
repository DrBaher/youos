"""Confidence × stakes → an action decision.

Whether to *act* on a draft autonomously isn't just "is the draft good." It's
the draft's quality (already draft-aware via ``quality_score`` — empty-output,
cloud fallback, and low voice-match all drag it down) crossed with the
**stakes** of the message. A flawless draft to a lawyer about a contract should
still go to a human; a good draft confirming a coffee time can act on its own.

This module maps that 2-D space to one of four actions (increasing human
involvement):

* ``auto_act`` — confident **and** low-stakes: eligible for an autonomous action.
* ``queue``    — draft and queue for review (today's default).
* ``ask``      — explicitly put the decision to the human (high stakes).
* ``skip``     — nothing worth doing.

High stakes is a hard veto on ``auto_act`` — money, legal, contracts, and the
like never auto-send, regardless of how good the draft looks. Pure and
deterministic; the autonomous send path (a later step) consumes the verdict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Money / legal / commitment language. A match means a human should decide,
# even on a high-quality draft. Word-boundaried so "contractor" doesn't trip
# "contract" and "legalese" doesn't trip "legal".
# NOTE: "confidential" was REMOVED (2026-06-22) — it's dominated by boilerplate
# email-disclaimer footers ("This email is confidential and may be privileged…")
# present on most corporate mail, so it held plenty of pure-scheduling replies
# (a meeting reply from M42 with a confidentiality footer). Genuine money/legal
# is still caught by the 20+ actionable terms below.
_HIGH_STAKES_PAT = re.compile(
    r"\b("
    r"contract|agreement|nda|non-disclosure|invoice|payment|pay|wire|"
    r"transfer|refund|deposit|salary|compensation|equity|offer letter|"
    r"lawsuit|legal|attorney|lawyer|counsel|liability|litigation|"
    r"terminate|termination|resign|settlement|"
    r"purchase order|deadline|overdue|past due|penalty|breach"
    r")\b",
    re.IGNORECASE,
)
# Currency amounts are high-stakes regardless of keywords.
_MONEY_AMOUNT_PAT = re.compile(r"[$€£]\s?\d|\b\d[\d,.]*\s?(?:USD|EUR|GBP|k\b)")

DEFAULT_AUTO_ACT_FLOOR = 0.8       # draft quality required to auto-act
DEFAULT_CONFIDENCE_FLOOR = 0.85    # needs-reply / calibrated confidence required


@dataclass
class ActionDecision:
    action: str                          # 'auto_act' | 'queue' | 'ask' | 'skip'
    stakes: str                          # 'high' | 'low'
    reasons: list[str] = field(default_factory=list)


def assess_stakes(subject: str | None, body: str | None) -> str:
    """``'high'`` if the message touches money / legal / firm commitments,
    else ``'low'``. Subject and body are both scanned."""
    text = f"{subject or ''}\n{body or ''}"
    if _HIGH_STAKES_PAT.search(text) or _MONEY_AMOUNT_PAT.search(text):
        return "high"
    return "low"


def decide_action(
    *,
    quality_score: float | None,
    needs_reply_score: float,
    calibrated_score: float | None = None,
    subject: str | None = "",
    body: str | None = "",
    auto_act_floor: float = DEFAULT_AUTO_ACT_FLOOR,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
    high_stakes_blocks: bool = True,
) -> ActionDecision:
    """Decide what to do with a drafted reply.

    ``calibrated_score`` (empirical P(deserved a reply), when a calibrator is
    fitted) is preferred over the raw ``needs_reply_score`` as the confidence
    signal. High stakes forces ``ask`` and can never reach ``auto_act``.
    """
    stakes = assess_stakes(subject, body)
    q = quality_score if quality_score is not None else 0.0
    conf = calibrated_score if calibrated_score is not None else needs_reply_score
    reasons: list[str] = []

    if stakes == "high" and high_stakes_blocks:
        return ActionDecision(
            "ask", stakes,
            ["high-stakes content (money/legal/commitment) — human decision required"],
        )

    if q < auto_act_floor:
        reasons.append(f"draft quality {q:.2f} < auto-act floor {auto_act_floor:.2f}")
        return ActionDecision("queue", stakes, reasons)

    if conf < confidence_floor:
        reasons.append(f"confidence {conf:.2f} < floor {confidence_floor:.2f}")
        return ActionDecision("queue", stakes, reasons)

    reasons.append(
        f"confident (quality {q:.2f}, confidence {conf:.2f}) and low-stakes"
    )
    return ActionDecision("auto_act", stakes, reasons)


def escalation_config() -> dict[str, float | bool]:
    """Read ``agent.escalation.*`` tuning with safe, conservative defaults."""
    from app.core.config import load_config

    cfg = load_config() or {}
    a = (cfg.get("agent") or {}) if isinstance(cfg, dict) else {}
    e = (a.get("escalation") or {}) if isinstance(a, dict) else {}
    if not isinstance(e, dict):
        e = {}

    def _f(key, default):
        try:
            return float(e.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "auto_act_floor": _f("auto_act_floor", DEFAULT_AUTO_ACT_FLOOR),
        "confidence_floor": _f("confidence_floor", DEFAULT_CONFIDENCE_FLOOR),
        "high_stakes_blocks": bool(e.get("high_stakes_blocks", True)),
    }
