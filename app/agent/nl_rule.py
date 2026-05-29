"""Natural-language → structured rule parsing.

Lets a user describe a rule in plain English ("archive newsletters older than a
week", "label anything from my accountant jane@books.com as Finance") and turns
it into the structured ``{match, action, value}`` shape the rule engine
understands — via the warm local model (no egress).

The parse NEVER saves. The caller confirms before persisting: the authoring UI
pre-fills the rule builder with the result so the user reviews (and can edit) it
before hitting Save. Failure-isolated — model unavailable or an unparseable
answer just returns ``ok=False`` with a message; it never raises into the page.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_MAX_TOKENS = 220

# Few-shot prompt. We advertise only the NL-friendly predicates (intent is
# omitted — it's awkward to phrase and rejected for routing actions anyway).
_PROMPT = """You convert a user's plain-English email rule into a JSON object.

Output ONLY a single JSON object, no prose and no code fences, with exactly three keys: "match", "action", "value".

"match" is an object containing ONLY the conditions the user actually stated. Allowed condition keys:
- sender: an exact email address (string)
- domain: the sender's domain like "@example.com" (string)
- subject_contains / body_contains / to_contains / cc_contains: a keyword or list of keywords (string or array of strings)
- subject_regex / body_regex: a regular expression (string)
- has_attachment: true or false
- known_contact: true or false (whether I have emailed with them before)
- cold_outreach: true or false (an unsolicited stranger)
- older_than_days / newer_than_days: a number of days

"action" is exactly one of: skip, decline, prepend, hold, label, archive, star, mark_read, mark_important, mark_unimportant.
"value" is the label name (only for action "label") or the instruction text (only for action "prepend"); otherwise null.

Examples:
User: archive newsletters older than a week
JSON: {"match": {"subject_contains": "newsletter", "older_than_days": 7}, "action": "archive", "value": null}

User: label anything from my accountant jane@books.com as Finance
JSON: {"match": {"sender": "jane@books.com"}, "action": "label", "value": "Finance"}

User: hold any email mentioning a contract or lawsuit so I can review it
JSON: {"match": {"body_contains": ["contract", "lawsuit"]}, "action": "hold", "value": null}

User: star messages with an attachment from strangers I don't know
JSON: {"match": {"has_attachment": true, "known_contact": false}, "action": "star", "value": null}

User: {text}
JSON:"""


def _extract_json(text: str | None) -> dict[str, Any] | None:
    """Pull the first balanced ``{...}`` object out of the model output (which
    may wrap it in prose or a code fence). String-aware so a regex value
    containing braces (e.g. ``\\d{3}``) doesn't throw off the brace count."""
    if not text:
        return None
    s = text.strip()
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(s[start:i + 1])
                except json.JSONDecodeError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def _coerce(rule: dict[str, Any]) -> dict[str, Any]:
    """Best-effort fix-ups for a model that stringifies types: "true"/"false" →
    bool, a numeric ``*_days`` string → number, action → lowercased. Keeps the
    structured-validator (which now requires real bools/finite numbers) happy
    without reaching into its private key groups."""
    match = rule.get("match")
    if isinstance(match, dict):
        for k, v in list(match.items()):
            if isinstance(v, str):
                lv = v.strip().lower()
                if lv in ("true", "false"):
                    match[k] = lv == "true"
                elif k.endswith("_days"):
                    try:
                        match[k] = float(v) if "." in v else int(v)
                    except ValueError:
                        pass
    if isinstance(rule.get("action"), str):
        rule["action"] = rule["action"].strip().lower()
    rule.setdefault("value", None)
    return rule


def parse_rule_text(text: str, *, complete_fn=None) -> dict[str, Any]:
    """Parse ``text`` into a structured rule. Returns
    ``{"ok": bool, "rule": dict|None, "error": str}`` and NEVER raises.

    ``ok`` is True only when the parsed rule also passes ``validate_rule``. Even
    when ``ok`` is False a best-effort ``rule`` may be returned (so the UI can
    pre-fill the builder and let the user fix it). ``complete_fn`` is injectable
    for tests; by default it routes to the warm local model (temperature 0)."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "rule": None, "error": "describe the rule in a sentence first"}

    if complete_fn is None:
        from app.core import model_server

        if not model_server.is_enabled():
            return {"ok": False, "rule": None,
                    "error": "the local model isn't running — build the rule manually below"}

        def complete_fn(p: str) -> str:
            return model_server.complete(p, max_tokens=_MAX_TOKENS, temperature=0.0)

    try:
        out = complete_fn(_PROMPT.replace("{text}", text))
    except Exception as exc:
        logger.info("NL rule parse skipped (model unavailable): %s", exc)
        return {"ok": False, "rule": None, "error": "couldn't reach the local model"}

    parsed = _extract_json(out)
    if not parsed or not isinstance(parsed.get("match"), dict):
        return {"ok": False, "rule": None,
                "error": "couldn't turn that into a rule — try rephrasing, or build it manually"}

    from app.agent.rules import validate_rule

    rule = _coerce(parsed)
    ok, err = validate_rule(rule)
    return {"ok": ok, "rule": rule, "error": "" if ok else err}
