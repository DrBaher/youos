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

A rule's ``match`` conditions are ANDed. Supported keys:
  ``sender`` (exact email), ``domain`` (``@x.com``), ``intent`` (an intent
  label), ``cold_outreach`` (bool);
  the keyword predicates ``subject_contains`` / ``body_contains`` /
  ``to_contains`` / ``cc_contains`` (a keyword or list of keywords,
  case-insensitive substring; matches if ANY hits);
  the regex predicates ``subject_regex`` / ``body_regex`` (case-insensitive
  ``re.search``);
  ``has_attachment`` (bool — does the message carry a real attachment),
  ``known_contact`` (bool — do I have prior reply pairs with this sender), and
  the recency predicates ``older_than_days`` / ``newer_than_days`` (message age
  in days from its ``Date`` header).
Draft-shaping actions: ``skip`` (don't draft), ``decline`` (draft a polite
decline), ``prepend`` (inject ``value`` into that draft's standing instructions),
and ``hold`` (draft + queue for review, but **never auto-act** — the human
decides). Mailbox-routing actions (applied by the agent-action framework to
every fetched message): ``label`` / ``archive`` / ``star`` / ``mark_read`` /
``mark_important`` / ``mark_unimportant`` — each a reversible Gmail label
mutation, gated + dry-run by default + undoable. Outbound action: ``forward``
(``value`` = destination email) — SENDS the message on; crosses the never-send
frontier, so it is gated behind the send frontier (``agent.send.enabled`` +
outbound kill-switch) PLUS a dedicated ``agent.actions.allow_forward`` opt-in,
is irreversible (no undo), and at-most-once.

    agent:
      rules:
        - match: {body_contains: [legal, contract, lawsuit]}
          action: hold
        - match: {subject_contains: invoice}
          action: hold

All actions stay within the never-act boundary: ``hold`` still drafts (so the
reply is ready) but excludes the row from auto-push/auto-send, so a person
always finishes-and-sends anything matching a hold rule.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

# Canned instruction for the common "decline" action.
DECLINE_INSTRUCTION = "Politely decline this request. Keep it short, courteous, and clear."

# Draft-shaping actions (operate on the reply / whether to draft).
_DRAFT_ACTIONS = ("skip", "decline", "prepend", "hold")
# Mailbox-routing actions (operate on the inbound message itself — applied by
# the agent-action framework to EVERY fetched message, not just drafts). Each
# maps to a reversible Gmail label add/remove (see actions._action_to_labels):
#   label            — add a Gmail label (value = label name; created if missing)
#   archive          — remove it from the inbox (route out)
#   star             — flag it (add STARRED)
#   mark_read        — clear the unread flag (remove UNREAD)
#   mark_important   — add to the Important tab (add IMPORTANT)
#   mark_unimportant — remove from the Important tab (remove IMPORTANT)
# (Outbound tasks like forward stay behind the send frontier; destructive ones
# like trash are intentionally not exposed here.)
_MAILBOX_ACTIONS = ("label", "archive", "star", "mark_read", "mark_important", "mark_unimportant")
# Public alias so callers (e.g. triage's routing-enable gate) test membership
# against the single source of truth instead of a hand-copied tuple that drifts.
MAILBOX_ACTIONS = _MAILBOX_ACTIONS
# Outbound actions — these SEND mail (cross the never-send frontier), so they
# are evaluated/executed on a SEPARATE path from the reversible label ops and
# gated behind the send frontier (agent.send.enabled + outbound kill-switch)
# PLUS a dedicated opt-in (agent.actions.allow_forward). They are irreversible
# (no undo) and at-most-once.
#   forward — forward the message to another address (value = destination email)
_OUTBOUND_ACTIONS = ("forward",)
OUTBOUND_ACTIONS = _OUTBOUND_ACTIONS
_VALID_ACTIONS = _DRAFT_ACTIONS + _MAILBOX_ACTIONS + _OUTBOUND_ACTIONS

# Reserved label namespace owned by gmail_label_sync (label→dismissal feedback).
# A routing rule must never target these or it would fight the sync that
# removes them (re-added every sweep). Prefix-matched, case-insensitive.
_RESERVED_LABEL_PREFIX = "youos/"


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
        norm = normalize_rule(r)
        if norm is not None:
            out.append(norm)
    return out


# Recognised match keys (so the authoring API can reject typos with a clear
# error instead of silently never matching). Grouped by the kind of value each
# expects, so validate_rule can check shape (regex compiles, ages are numbers,
# flags are booleans) rather than only the key name.
_BOOL_KEYS = ("cold_outreach", "has_attachment", "known_contact")
_NUMERIC_KEYS = ("older_than_days", "newer_than_days")
_REGEX_KEYS = ("subject_regex", "body_regex")
# keyword (string or list of strings, substring) predicates
_KEYWORD_KEYS = ("subject_contains", "body_contains", "to_contains", "cc_contains")
MATCH_KEYS = (
    "sender", "domain", "intent",
    *_KEYWORD_KEYS, *_BOOL_KEYS, *_NUMERIC_KEYS, *_REGEX_KEYS,
)


def validate_rule(raw: Any) -> tuple[bool, str]:
    """Validate a single rule dict for the authoring API. Returns (ok, error)."""
    if not isinstance(raw, dict):
        return False, "rule must be an object"
    match = raw.get("match")
    if not isinstance(match, dict) or not match:
        return False, "rule.match must be a non-empty object"
    unknown = [k for k in match if k not in MATCH_KEYS]
    if unknown:
        return False, f"unknown match key(s): {unknown}; allowed: {list(MATCH_KEYS)}"
    for k in _REGEX_KEYS:
        if k in match:
            # Reject a non-string / empty pattern up front: str(None) would compile
            # to the literal pattern 'None' and silently match the word "None".
            if not isinstance(match[k], str) or not match[k]:
                return False, f"{k} must be a non-empty regex string"
            try:
                re.compile(match[k])
            except re.error as e:
                return False, f"{k} is not a valid regular expression: {e}"
    for k in _NUMERIC_KEYS:
        if k in match:
            try:
                v = float(match[k])
            except (TypeError, ValueError):
                return False, f"{k} must be a number of days"
            # math.isfinite rejects NaN/Infinity: NaN would make every age
            # comparison False, silently turning the recency clause into a
            # no-op that widens the match (e.g. archiving fresh mail).
            if not math.isfinite(v) or v < 0:
                return False, f"{k} must be a finite, non-negative number of days"
    for k in _BOOL_KEYS:
        # A quoted YAML string ("false") is truthy under bool(), inverting the
        # predicate; require a real boolean so the save-time gate catches it.
        if k in match and not isinstance(match[k], bool):
            return False, f"{k} must be true or false"
    action = str(raw.get("action", "")).strip().lower()
    if action not in _VALID_ACTIONS:
        return False, f"unknown action {action!r}; allowed: {list(_VALID_ACTIONS)}"
    if action in (_MAILBOX_ACTIONS + _OUTBOUND_ACTIONS) and "intent" in match:
        # Routing (label/archive/star/...) and outbound (forward) run before
        # per-message intent classification, so an 'intent' predicate would
        # never fire there — reject it rather than save a rule that does nothing.
        return False, "the 'intent' predicate is not supported for routing/forward rules (they run before intent classification)"
    if action == "label":
        name = str(raw.get("value") or "").strip()
        if not name:
            return False, "a 'label' rule needs a non-empty 'value' (the label name)"
        if "," in name:
            return False, "a label name cannot contain a comma"
        if name.lower().startswith(_RESERVED_LABEL_PREFIX):
            return False, f"label names starting with {_RESERVED_LABEL_PREFIX!r} are reserved"
    if action == "forward":
        dest = str(raw.get("value") or "").strip()
        if not dest:
            return False, "a 'forward' rule needs a non-empty 'value' (the destination email address)"
        recipients = [p.strip() for p in dest.split(",") if p.strip()]
        if not recipients or not all(_looks_like_email(p) for p in recipients):
            return False, "a 'forward' rule's 'value' must be one or more valid email addresses (comma-separated)"
    return True, ""


# A deliberately simple address check (not full RFC 5322): local@domain.tld with
# no spaces/commas in either side. Enough to reject typos before a real send.
_EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")


def _looks_like_email(s: str) -> bool:
    return bool(_EMAIL_RE.match(s.strip()))


def normalize_rule(raw: Any) -> dict[str, Any] | None:
    """Validate + canonicalise one rule, or None if invalid (load_rules drops
    invalid rules silently; the API uses validate_rule for errors)."""
    ok, _ = validate_rule(raw)
    if not ok:
        return None
    return {
        "match": raw["match"],
        "action": str(raw["action"]).strip().lower(),
        "value": raw.get("value"),
    }


def save_rules(rules: list[dict[str, Any]], *, config_path: Path | None = None) -> list[dict[str, Any]]:
    """Persist the full ``agent.rules`` list to config (validated). Returns the
    saved (normalised) rules. The single write path the authoring API uses."""
    import copy

    from app.core.config import load_config, save_config

    normalised = [normalize_rule(r) for r in rules]
    if any(n is None for n in normalised):
        bad = next(i for i, n in enumerate(normalised) if n is None)
        raise ValueError(f"rule at index {bad} is invalid")
    cfg = copy.deepcopy(load_config(config_path) or {})
    agent = cfg.setdefault("agent", {})
    if not isinstance(agent, dict):
        agent = {}
        cfg["agent"] = agent
    agent["rules"] = normalised
    save_config(cfg, config_path)
    return normalised


def rules_need_intent(rules: list[dict[str, Any]]) -> bool:
    """Whether any rule matches on intent (so the caller knows to classify)."""
    return any("intent" in (r.get("match") or {}) for r in rules)


def evaluate_mailbox_actions(
    rules: list[dict[str, Any]],
    *,
    sender_email: str | None,
    domain: str | None,
    subject: str | None,
    body: str | None,
    intents: list[str] | None = None,
    cold_outreach: bool = False,
    to: str | None = None,
    cc: str | None = None,
    has_attachment: bool = False,
    age_days: float | None = None,
    known_contact: bool = False,
) -> list[dict[str, Any]]:
    """Return the mailbox-routing actions (label/archive/star) whose match fires
    for this message. Runs on EVERY fetched message (routing isn't tied to
    drafting). Each entry is ``{"type": ..., "value": <label name or None>}``;
    duplicates (same type+value) are collapsed."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for r in rules:
        action = r["action"]
        if action not in _MAILBOX_ACTIONS:
            continue
        if not _rule_matches(
            r["match"], sender_email=sender_email, domain=domain,
            intents=intents, cold_outreach=cold_outreach, subject=subject, body=body,
            to=to, cc=cc, has_attachment=has_attachment, age_days=age_days,
            known_contact=known_contact,
        ):
            continue
        value = str(r.get("value") or "").strip() or None
        if action == "label" and not value:
            continue  # a label rule with no label name is a no-op
        key = (action, value or "")
        if key in seen:
            continue
        seen.add(key)
        out.append({"type": action, "value": value})
    return out


def evaluate_outbound_actions(
    rules: list[dict[str, Any]],
    *,
    sender_email: str | None,
    domain: str | None,
    subject: str | None,
    body: str | None,
    intents: list[str] | None = None,
    cold_outreach: bool = False,
    to: str | None = None,
    cc: str | None = None,
    has_attachment: bool = False,
    age_days: float | None = None,
    known_contact: bool = False,
) -> list[dict[str, Any]]:
    """Return the OUTBOUND actions (forward) whose match fires for this message.
    Kept on a separate path from ``evaluate_mailbox_actions`` because forwarding
    SENDS mail — its executor is gated behind the send frontier + a dedicated
    opt-in and is irreversible. Each entry is ``{"type": "forward", "value":
    <destination>}``; a forward with no destination is dropped; dupes collapse."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for r in rules:
        action = r["action"]
        if action not in _OUTBOUND_ACTIONS:
            continue
        if not _rule_matches(
            r["match"], sender_email=sender_email, domain=domain,
            intents=intents, cold_outreach=cold_outreach, subject=subject, body=body,
            to=to, cc=cc, has_attachment=has_attachment, age_days=age_days,
            known_contact=known_contact,
        ):
            continue
        value = str(r.get("value") or "").strip() or None
        if not value:
            continue  # a forward with no destination is a no-op (and unsafe)
        key = (action, value)
        if key in seen:
            continue
        seen.add(key)
        out.append({"type": action, "value": value})
    return out


def _any_keyword_in(value: Any, haystack: str) -> bool:
    """True if any keyword (a string or list of strings) is a case-insensitive
    substring of ``haystack``."""
    h = (haystack or "").lower()
    needles = value if isinstance(value, (list, tuple)) else [value]
    return any(str(n).strip().lower() in h for n in needles if str(n).strip())


# Cap regex input length so a non-pathological pattern can't run unbounded over
# a very large body. Rule regexes are OPERATOR-trusted (only the user authors
# them) and patterns are NOT ReDoS-analysed — re.compile alone can't detect
# catastrophic backtracking — so a deliberately nested-quantifier pattern is a
# self-inflicted foot-gun; this cap just bounds the common large-body case.
_REGEX_HAYSTACK_CAP = 8000


def _regex_search(pattern: Any, haystack: str) -> bool:
    try:
        return re.search(str(pattern), (haystack or "")[:_REGEX_HAYSTACK_CAP], re.IGNORECASE) is not None
    except re.error:
        return False  # invalid regex (validate_rule rejects these at save time)


def _rule_matches(
    match: dict[str, Any],
    *,
    sender_email: str | None,
    domain: str | None,
    intents: list[str] | None,
    cold_outreach: bool,
    subject: str | None = None,
    body: str | None = None,
    to: str | None = None,
    cc: str | None = None,
    has_attachment: bool = False,
    age_days: float | None = None,
    known_contact: bool = False,
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
    if "subject_contains" in match and not _any_keyword_in(match["subject_contains"], subject or ""):
        return False
    if "body_contains" in match and not _any_keyword_in(match["body_contains"], body or ""):
        return False
    if "to_contains" in match and not _any_keyword_in(match["to_contains"], to or ""):
        return False
    if "cc_contains" in match and not _any_keyword_in(match["cc_contains"], cc or ""):
        return False
    if "subject_regex" in match and not _regex_search(match["subject_regex"], subject or ""):
        return False
    if "body_regex" in match and not _regex_search(match["body_regex"], body or ""):
        return False
    if "has_attachment" in match and bool(match["has_attachment"]) != bool(has_attachment):
        return False
    if "known_contact" in match and bool(match["known_contact"]) != bool(known_contact):
        return False
    if "older_than_days" in match:
        try:
            if age_days is None or age_days < float(match["older_than_days"]):
                return False
        except (TypeError, ValueError):
            return False
    if "newer_than_days" in match:
        try:
            if age_days is None or age_days > float(match["newer_than_days"]):
                return False
        except (TypeError, ValueError):
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
    subject: str | None = None,
    body: str | None = None,
    to: str | None = None,
    cc: str | None = None,
    has_attachment: bool = False,
    age_days: float | None = None,
    known_contact: bool = False,
) -> dict[str, Any]:
    """Evaluate ``rules`` for one message.

    Returns ``{skip: bool, hold: bool, instructions: str|None, matched: list}``.
    ``instructions`` folds the global ``base_instructions`` together with any
    matched rules' instructions. ``skip`` is True if any matched rule says skip
    (don't draft). ``hold`` is True if any matched rule says hold (draft, but
    never auto-act — the row is excluded from auto-push/auto-send).
    """
    extra: list[str] = []
    skip = False
    hold = False
    matched: list[dict[str, Any]] = []
    for r in rules:
        if not _rule_matches(
            r["match"], sender_email=sender_email, domain=domain,
            intents=intents, cold_outreach=cold_outreach,
            subject=subject, body=body,
            to=to, cc=cc, has_attachment=has_attachment, age_days=age_days,
            known_contact=known_contact,
        ):
            continue
        matched.append(r)
        if r["action"] == "skip":
            skip = True
        elif r["action"] == "hold":
            hold = True
        elif r["action"] == "decline":
            extra.append(DECLINE_INSTRUCTION)
        elif r["action"] == "prepend":
            v = str(r.get("value") or "").strip()
            if v:
                extra.append(v)
    combined = [s for s in ([base_instructions] if base_instructions else []) + extra if s]
    return {
        "skip": skip,
        "hold": hold,
        "instructions": "\n".join(combined) if combined else None,
        "matched": matched,
    }
