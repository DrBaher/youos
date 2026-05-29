"""Structured standing-instruction rules.

``agent.standing_instructions`` is one free-form string prepended to every
draft — a blunt instrument. This adds durable, conditional rules so the agent
follows policies, not just a global hint: "always decline recruiter outreach",
"for client X, always note that I'll CC my partner and confirm the timeline",
"for meeting requests, propose Tue/Thu afternoons".

Rules live under ``agent.rules`` in ``youos_config.yaml`` (a list, edited
directly — not a scalar feature flag):

    agent:
      rules:
        - match: {domain: "@recruiters.com"}
          action: decline
        - match: {sender: "client@bigco.com"}
          action: prepend
          value: "Note that I'll CC my partner jane@me.com and confirm the timeline."
        - match: {intent: meeting_request}
          action: prepend
          value: "Propose Tue or Thu afternoons in my timezone."
        - match: {cold_outreach: true}
          action: skip

A rule's ``match`` conditions are ANDed. Supported keys: ``sender`` (exact
email), ``domain`` (``@x.com``), ``intent`` (an intent label), ``cold_outreach``
(bool). Actions: ``skip`` (don't draft), ``decline`` (draft a polite decline),
``prepend`` (inject ``value`` into that draft's standing instructions). All
actions stay draft-only — the human still finishes-and-sends.
"""

from __future__ import annotations

from typing import Any

# Canned instruction for the common "decline" action.
DECLINE_INSTRUCTION = "Politely decline this request. Keep it short, courteous, and clear."

_VALID_ACTIONS = ("skip", "decline", "prepend")


def load_rules() -> list[dict[str, Any]]:
    """Read + normalise ``agent.rules`` from config. Returns [] on any problem
    (malformed config must never break the sweep)."""
    try:
        from app.core.config import load_config

        cfg = load_config() or {}
        a = cfg.get("agent", {}) if isinstance(cfg, dict) else {}
        raw = a.get("rules") if isinstance(a, dict) else None
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        match = r.get("match")
        action = str(r.get("action", "")).strip().lower()
        if not isinstance(match, dict) or not match or action not in _VALID_ACTIONS:
            continue
        out.append({"match": match, "action": action, "value": r.get("value")})
    return out


def rules_need_intent(rules: list[dict[str, Any]]) -> bool:
    """Whether any rule matches on intent (so the caller knows to classify)."""
    return any("intent" in (r.get("match") or {}) for r in rules)


def _rule_matches(
    match: dict[str, Any],
    *,
    sender_email: str | None,
    domain: str | None,
    intents: list[str] | None,
    cold_outreach: bool,
) -> bool:
    se = (sender_email or "").lower()
    dom = (domain or "").lower()
    if "sender" in match and se != str(match["sender"]).strip().lower():
        return False
    if "domain" in match:
        d = str(match["domain"]).strip().lower()
        bare = d.lstrip("@")
        if not (se.endswith(d) or se.endswith("@" + bare) or dom == bare):
            return False
    if "intent" in match:
        want = str(match["intent"]).strip().lower()
        if want not in [str(i).lower() for i in (intents or [])]:
            return False
    if "cold_outreach" in match and bool(match["cold_outreach"]) != bool(cold_outreach):
        return False
    return True


def apply_rules(
    rules: list[dict[str, Any]],
    *,
    sender_email: str | None,
    domain: str | None,
    intents: list[str] | None,
    cold_outreach: bool,
    base_instructions: str | None,
) -> dict[str, Any]:
    """Evaluate ``rules`` for one message.

    Returns ``{skip: bool, instructions: str|None, matched: list}``.
    ``instructions`` folds the global ``base_instructions`` together with any
    matched rules' instructions (so it's the complete per-message standing
    instruction). ``skip`` is True if any matched rule says skip.
    """
    extra: list[str] = []
    skip = False
    matched: list[dict[str, Any]] = []
    for r in rules:
        if not _rule_matches(
            r["match"], sender_email=sender_email, domain=domain,
            intents=intents, cold_outreach=cold_outreach,
        ):
            continue
        matched.append(r)
        if r["action"] == "skip":
            skip = True
        elif r["action"] == "decline":
            extra.append(DECLINE_INSTRUCTION)
        elif r["action"] == "prepend":
            v = str(r.get("value") or "").strip()
            if v:
                extra.append(v)
    combined = [s for s in ([base_instructions] if base_instructions else []) + extra if s]
    return {
        "skip": skip,
        "instructions": "\n".join(combined) if combined else None,
        "matched": matched,
    }
