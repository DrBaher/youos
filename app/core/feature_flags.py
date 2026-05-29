"""Whitelisted feature flags — the single set of toggles surfaced by the
`youos config` CLI, the settings page, and the onboarding wizard.

Restricting writes to this whitelist is what makes a (web) config-write path
safe: it can only touch these known keys, never clobber arbitrary config.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from app.core.config import load_config, save_config

# Each flag: key (dotted), label, type (bool|choice), default, help; choice
# flags also carry `choices`. Keep this list in sync with the config schema.
KNOWN_FLAGS: list[dict[str, Any]] = [
    {
        "key": "generation.multi_candidate.enabled",
        "label": "Multi-candidate drafting",
        "type": "bool",
        "default": False,
        "help": "Generate several drafts and keep the best (slower; more model calls).",
    },
    {
        "key": "generation.repair.enforce_greeting_closing",
        "label": "Enforce greeting/closing",
        "type": "bool",
        "default": False,
        "help": "Add the persona greeting/closing if the model dropped them.",
    },
    {
        "key": "generation.repair.strip_trailing_signature",
        "label": "Strip trailing signature",
        "type": "bool",
        "default": False,
        "help": "Remove a duplicate sign-off from the generated draft.",
    },
    {
        "key": "generation.log_drafts",
        "label": "Log draft events",
        "type": "bool",
        "default": True,
        "help": "Record each draft's conditions so the nightly can learn from them.",
    },
    {
        "key": "autoresearch.draft_quality_weighting",
        "label": "Draft-quality autoresearch weighting",
        "type": "bool",
        "default": False,
        "help": "Tune retrieval toward the sender cohorts whose drafts you edit most.",
    },
    {
        "key": "personas.routing_enabled",
        "label": "Per-persona generation routing",
        "type": "bool",
        "default": False,
        "help": "Use a per-sender-type LoRA adapter when one is trained.",
    },
    {
        "key": "review.draft_model",
        "label": "Drafting model",
        "type": "choice",
        "default": "auto",
        "choices": ["auto", "local", "claude"],
        "help": "Which model drafts: 'auto' = local fine-tuned model when trained (else Claude); 'local' = always local; 'claude' = always cloud.",
    },
    {
        "key": "model.server.enabled",
        "label": "Warm local-model server",
        "type": "bool",
        "default": True,
        "help": "Keep the local model loaded once (served warm) so drafts are fast. Apple Silicon only; falls back to cloud if unavailable.",
    },
    {
        "key": "ingestion.google_backend",
        "label": "Google ingestion backend",
        "type": "choice",
        "default": "gog",
        "choices": ["gog", "gws", "native"],
        "help": "Which tool fetches Gmail/Docs: the OpenClaw gog CLI, Google's gws CLI, or the native API.",
    },
    {
        "key": "agent.enabled",
        "label": "Autonomous triage",
        "type": "bool",
        "default": False,
        "help": "Sweep your unread inbox in the background and draft replies into /triage. Never auto-sends. Off by default — opt-in.",
    },
    {
        "key": "agent.interval_minutes",
        "label": "Triage interval (minutes)",
        "type": "int",
        "default": 15,
        "help": "How often the background loop sweeps unread mail. Minimum 1; the loop enforces a 60-second floor for safety.",
    },
    {
        "key": "agent.threshold",
        "label": "Needs-reply threshold",
        "type": "float",
        "default": 0.6,
        "min": 0.4,
        "max": 0.85,
        "help": (
            "Score cutoff for drafting a reply. Higher = stricter (fewer "
            "false positives, but more real mail missed); lower = looser. "
            "Clamped to 0.4–0.85. Prefer feedback-driven tuning over manual "
            "tweaks. Read by the background scheduler and /api/agent/triage."
        ),
    },
    {
        "key": "agent.notify_macos",
        "label": "macOS notification on new drafts",
        "type": "bool",
        "default": True,
        "help": "Fire a desktop notification when a triage sweep persists new drafts. Silently no-ops on non-Darwin.",
    },
    {
        "key": "agent.standing_instructions",
        "label": "Standing instructions",
        "type": "text",
        "default": "",
        "help": (
            "Free-form guidance threaded into every triage draft "
            "(e.g. \"today I'm out of office; politely decline meetings\"). "
            "Snapshotted with each draft for auditability. Empty = none."
        ),
    },
    {
        "key": "agent.skip_senders",
        "label": "Never draft for these senders",
        "type": "text",
        "default": "",
        "help": (
            "Comma-separated emails or @domains the agent skips outright. "
            "Use exact emails (alice@x.com) or @domain (@bigcorp.com) to "
            "skip a whole org. Hard-skip; runs before scoring."
        ),
    },
    {
        "key": "agent.daily_draft_cap",
        "label": "Daily draft cap",
        "type": "int",
        "default": 50,
        "help": (
            "Maximum new drafts the agent will persist in one UTC day, "
            "per account. Defends against a runaway loop on a noisy "
            "inbox. Set to 0 to disable."
        ),
    },
    {
        "key": "agent.strict_local",
        "label": "Strict local-only (no cloud fallback during triage)",
        "type": "bool",
        "default": False,
        "help": (
            "Refuse cloud fallback during background triage — if the "
            "local model is unavailable, the message is logged as an "
            "error rather than drafted via Claude. Doesn't affect "
            "interactive /feedback or /draft."
        ),
    },
    {
        "key": "agent.accounts",
        "label": "Accounts to sweep (override user.emails)",
        "type": "text",
        "default": "",
        "help": (
            "Comma-separated email addresses the background scheduler "
            "should sweep on each tick. Empty falls back to "
            "``user.emails`` (most users don't need to set this — set "
            "user.emails and it Just Works). Use this if you want the "
            "agent to ignore one of your configured accounts."
        ),
    },
    {
        "key": "agent.auto_push.enabled",
        "label": "Auto-push high-confidence drafts to Gmail Drafts",
        "type": "bool",
        "default": False,
        "help": (
            "After a sweep, automatically create a Gmail DRAFT (never sends) for "
            "high-confidence replies to known, whitelisted senders. The human "
            "still finishes-and-sends from Gmail. Off by default; even when on, "
            "it stays in dry-run until you turn dry-run off."
        ),
    },
    {
        "key": "agent.auto_push.dry_run",
        "label": "Auto-push dry-run (log only)",
        "type": "bool",
        "default": True,
        "help": (
            "When on, auto-push only LOGS what it would push (no Gmail write) so "
            "you can watch it for a week before trusting it. Turn off to actually "
            "create the drafts."
        ),
    },
    {
        "key": "agent.auto_push.confidence_floor",
        "label": "Auto-push confidence floor",
        "type": "float",
        "default": 0.85,
        "min": 0.6,
        "max": 1.0,
        "help": "Only auto-push drafts whose needs-reply score is at least this. Clamped 0.6–1.0.",
    },
    {
        "key": "agent.auto_push.known_sender_min_pairs",
        "label": "Auto-push: min prior reply pairs with the sender",
        "type": "int",
        "default": 3,
        "help": "Only auto-push to senders you've already corresponded with at least this many times.",
    },
    {
        "key": "agent.auto_push.daily_push_cap",
        "label": "Auto-push daily cap (per account)",
        "type": "int",
        "default": 5,
        "help": "Maximum drafts auto-pushed per UTC day per account. Bounds blast radius. 0 disables auto-push.",
    },
    {
        "key": "agent.auto_push.whitelist",
        "label": "Auto-push sender whitelist",
        "type": "text",
        "default": "",
        "help": (
            "Comma-separated emails or @domains eligible for auto-push. REQUIRED "
            "— with an empty whitelist nothing is auto-pushed (safety). Use exact "
            "emails (alice@x.com) or @domain (@partner.com)."
        ),
    },
    {
        "key": "agent.notify_webhook_url",
        "label": "Proactive push webhook URL",
        "type": "text",
        "default": "",
        "help": (
            "When set, the agent POSTs a digest summary here after a sweep that "
            "has something actionable (change-detected + throttled) so you/your "
            "bot are nudged without polling. The ONE place YouOS makes an "
            "outbound request — metadata only (counts + truncated subjects), "
            "never message bodies. Empty = no push (default)."
        ),
    },
    {
        "key": "agent.notify_webhook_secret",
        "label": "Proactive push webhook secret",
        "type": "text",
        "default": "",
        "help": "Optional shared secret sent as the X-YouOS-Secret header so your receiver can verify the push is from YouOS.",
    },
    {
        "key": "agent.notify_min_interval_minutes",
        "label": "Min minutes between webhook pushes",
        "type": "int",
        "default": 10,
        "help": "Throttle: at most one webhook push per account per this many minutes, and only when the queue state changed.",
    },
    {
        "key": "agent.calendar.enabled",
        "label": "Propose meeting times from your calendar",
        "type": "bool",
        "default": False,
        "help": (
            "When the agent drafts a reply to a meeting request, read your "
            "calendar free/busy (via the gog CLI) and offer concrete open slots "
            "in the draft. Never creates events — proposes times you send. "
            "Off by default; needs the gog calendar scope authorized."
        ),
    },
    {
        "key": "agent.vip_senders",
        "label": "VIP senders (prioritized)",
        "type": "text",
        "default": "",
        "help": (
            "Comma-separated emails or @domains whose mail is prioritized — a "
            "strong needs-reply boost so it clears the threshold and sorts to "
            "the top of the queue. Their automation/newsletters are still "
            "hard-skipped. Use exact emails (cofounder@x.com) or @domain."
        ),
    },
    {
        "key": "agent.followup_owed_days",
        "label": "Follow-up: flag unanswered inbound after N days",
        "type": "int",
        "default": 2,
        "help": "A queued email you haven't acted on for this many days is flagged as 'owed' in the digest + /api/agent/followups.",
    },
    {
        "key": "agent.followup_wait_days",
        "label": "Follow-up: flag awaiting-reply after N days",
        "type": "int",
        "default": 4,
        "help": "A reply you pushed/sent with no newer thread activity for this many days is flagged as 'awaiting reply'.",
    },
    {
        "key": "agent.auto_promote_skip_senders",
        "label": "Auto-promote senders dismissed as noise 3+ times",
        "type": "bool",
        "default": False,
        "help": (
            "When a sender has been dismissed as 'noise' 3+ times in the "
            "last 30 days, automatically add them to skip_senders at the "
            "end of each sweep — no click required. Off by default; even "
            "with it on, the user can review promotions in the audit log "
            "and remove any in /settings."
        ),
    },
]

_BY_KEY: dict[str, dict[str, Any]] = {f["key"]: f for f in KNOWN_FLAGS}


def known_keys() -> list[str]:
    return [f["key"] for f in KNOWN_FLAGS]


def _get_dotted(cfg: dict, key: str, default: Any = None) -> Any:
    cur: Any = cfg
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _set_dotted(cfg: dict, key: str, value: Any) -> None:
    parts = key.split(".")
    cur = cfg
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def coerce_value(flag: dict, raw: Any) -> Any:
    """Coerce a raw (often string) value to the flag's type, or raise ValueError."""
    if flag["type"] == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"expected a boolean (true/false), got {raw!r}")
    if flag["type"] == "choice":
        s = str(raw).strip()
        if s not in flag["choices"]:
            raise ValueError(f"expected one of {flag['choices']}, got {raw!r}")
        return s
    if flag["type"] == "float":
        try:
            v = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"expected a number, got {raw!r}") from exc
        lo, hi = flag.get("min"), flag.get("max")
        if lo is not None:
            v = max(float(lo), v)
        if hi is not None:
            v = min(float(hi), v)
        return v
    return raw


def list_flags(config: dict | None = None) -> list[dict[str, Any]]:
    """All known flags with their current effective value (config or default)."""
    cfg = config if config is not None else load_config()
    return [{**f, "value": _get_dotted(cfg, f["key"], f["default"])} for f in KNOWN_FLAGS]


def get_flag(key: str, config: dict | None = None) -> Any:
    if key not in _BY_KEY:
        raise KeyError(key)
    cfg = config if config is not None else load_config()
    return _get_dotted(cfg, key, _BY_KEY[key]["default"])


def set_flag(key: str, raw_value: Any, *, config_path: Path | None = None) -> Any:
    """Validate + coerce + persist a flag. Returns the stored value.

    Raises ``KeyError`` for an unknown (non-whitelisted) key and ``ValueError``
    for a value that doesn't fit the flag's type.
    """
    if key not in _BY_KEY:
        raise KeyError(f"unknown flag {key!r}; known: {', '.join(known_keys())}")
    value = coerce_value(_BY_KEY[key], raw_value)
    cfg = copy.deepcopy(load_config(config_path))
    _set_dotted(cfg, key, value)
    save_config(cfg, config_path)
    return value


def derive_os_name(name: str | None) -> str:
    """Personalize the product name from the user's name: ``Baher`` → ``BaherOS``.

    The idea behind YouOS: during setup it becomes *your* OS. Uses the first name
    token, preserving its internal casing (so ``McAvoy`` → ``McAvoyOS``); empty
    input falls back to the generic ``YouOS``.
    """
    raw = (name or "").strip()
    if not raw:
        return "YouOS"
    first = raw.split()[0]
    return f"{first[0].upper()}{first[1:]}OS"


def set_identity(
    name: str | None = None,
    emails: list[str] | None = None,
    *,
    display_name: str | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Persist the user's identity (``user.name`` / ``user.emails`` / ``display_name``).

    Used by the onboarding wizard / settings. Not a feature flag, but the same
    controlled, validated write path. When a name is set, the display name is
    auto-derived as ``<First>OS`` (e.g. ``BaherOS``) — unless an explicit
    ``display_name`` is given, or the user has already chosen a custom one (we
    only update display names that still track the previous derived value).
    Returns the stored identity.
    """
    cfg = copy.deepcopy(load_config(config_path))
    user = cfg.setdefault("user", {})
    if not isinstance(user, dict):
        user = {}
        cfg["user"] = user
    old_name = user.get("name", "")
    if name is not None:
        user["name"] = str(name).strip()
    if emails is not None:
        if not isinstance(emails, list):
            raise ValueError("emails must be a list")
        user["emails"] = [str(e).strip() for e in emails if str(e).strip()]
    if display_name is not None:
        user["display_name"] = str(display_name).strip()
    elif name is not None:
        # Auto-derive <First>OS, but don't clobber a custom brand: only set it
        # when there's no display name yet or the current one still tracks the
        # old name's derived value.
        current = str(user.get("display_name", "")).strip()
        if not current or current == derive_os_name(old_name):
            user["display_name"] = derive_os_name(name)
    save_config(cfg, config_path)
    return {
        "name": user.get("name", ""),
        "emails": user.get("emails", []),
        "display_name": user.get("display_name", ""),
    }
