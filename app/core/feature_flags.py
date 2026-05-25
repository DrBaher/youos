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
        "key": "ingestion.google_backend",
        "label": "Google ingestion backend",
        "type": "choice",
        "default": "gog",
        "choices": ["gog", "gws", "native"],
        "help": "Which tool fetches Gmail/Docs: the OpenClaw gog CLI, Google's gws CLI, or the native API.",
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


def set_identity(
    name: str | None = None,
    emails: list[str] | None = None,
    *,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Persist the user's identity (``user.name`` / ``user.emails``).

    Used by the onboarding wizard / settings. Not a feature flag, but the same
    controlled, validated write path. Returns the stored identity.
    """
    cfg = copy.deepcopy(load_config(config_path))
    user = cfg.setdefault("user", {})
    if not isinstance(user, dict):
        user = {}
        cfg["user"] = user
    if name is not None:
        user["name"] = str(name).strip()
    if emails is not None:
        if not isinstance(emails, list):
            raise ValueError("emails must be a list")
        user["emails"] = [str(e).strip() for e in emails if str(e).strip()]
    save_config(cfg, config_path)
    return {"name": user.get("name", ""), "emails": user.get("emails", [])}
