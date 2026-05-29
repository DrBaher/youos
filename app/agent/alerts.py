"""Proactive alerting + failure classification for the autonomous agent.

A background agent that silently stops drafting — expired Google auth, a model
server that died and is serving empty drafts, a sweep crashing every tick — is
worse than no agent, because the user *thinks* it's working. This turns those
failure modes into actionable alerts instead of a line in a log file.

Two signals:

* **Failure classification** — when a sweep raises, map the error to a kind
  (``auth`` / ``network`` / ``rate_limit`` / ``unknown``) and a remediation the
  user can act on (e.g. "run: gog auth login").
* **Sweep health** — even a "successful" sweep is unhealthy if most drafts came
  from the cloud fallback (the local model is down) or came back empty. A spike
  past a threshold is alert-worthy.

Pure/deterministic; the scheduler decides when to actually fire (debounced).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Expired/absent Google OAuth — the single most likely silent-death cause.
_AUTH_PAT = re.compile(
    r"no auth for|gog auth|auth.*expired|invalid_grant|unauthor|401|403|"
    r"re-?authenticate|re-?auth|credentials|token.*expired|refresh.?error|"
    r"insufficient.{0,40}(scope|permission)|access.?denied",
    re.IGNORECASE,
)
_RATE_PAT = re.compile(
    r"rate.?limit|quota|429|too many requests|resource.?exhausted", re.IGNORECASE
)
_NETWORK_PAT = re.compile(
    r"timed out|timeout|connection|network|unreachable|temporarily|dns|getaddrinfo",
    re.IGNORECASE,
)

# A sweep is unhealthy if more than this fraction of its drafts fell back to the
# cloud (local model likely down) or came back empty. Only meaningful with a
# few drafts — see ``min_drafts``.
DEFAULT_FALLBACK_SPIKE = 0.5
DEFAULT_EMPTY_SPIKE = 0.5

# Sentinel model markers + placeholder draft prefixes that mean "no real model
# produced this" — these are broken-output, not a cloud fallback.
_BROKEN_MODELS = frozenset({"none", "error", "", "unavailable"})
_PLACEHOLDER_PREFIXES = ("[no model", "[draft generat", "[error", "[unavailable")


@dataclass
class FailureClass:
    kind: str           # 'auth' | 'rate_limit' | 'network' | 'unknown'
    title: str
    message: str


def classify_sweep_failure(detail: str | None) -> FailureClass:
    """Map a sweep error string to an actionable alert."""
    d = (detail or "").strip()
    if _AUTH_PAT.search(d):
        return FailureClass(
            "auth",
            "YouOS agent: re-authentication needed",
            "The agent can't reach Gmail — Google auth looks expired. "
            "Re-authenticate: gog auth login",
        )
    if _RATE_PAT.search(d):
        return FailureClass(
            "rate_limit",
            "YouOS agent: rate-limited",
            "Gmail is rate-limiting the agent; it will retry. If this persists, "
            "lower agent.interval_minutes or the sweep limit.",
        )
    if _NETWORK_PAT.search(d):
        return FailureClass(
            "network",
            "YouOS agent: network error",
            f"A network error stopped the sweep; it will retry. ({d[:120]})",
        )
    return FailureClass(
        "unknown",
        "YouOS agent stopped drafting",
        f"A sweep failed: {d[:140]}. Check `youos doctor`.",
    )


def sweep_health(
    drafts: list[Any],
    *,
    min_drafts: int = 3,
    fallback_spike: float = DEFAULT_FALLBACK_SPIKE,
    empty_spike: float = DEFAULT_EMPTY_SPIKE,
) -> dict[str, Any]:
    """Assess a completed sweep's drafts. ``drafts`` are TriageDraft-likes with
    ``model_used`` / ``draft`` / ``error`` attributes.

    Returns counts + rates + a ``spike`` dict flagging cloud-fallback / empty
    spikes. ``spike`` is all-False below ``min_drafts`` (too small to judge)."""
    total = len(drafts)
    cloud = 0
    empty = 0
    for d in drafts:
        model = (getattr(d, "model_used", None) or "")
        body = (getattr(d, "draft", None) or "").strip()
        err = getattr(d, "error", None)
        body_l = body.lower()
        is_placeholder = any(body_l.startswith(p) for p in _PLACEHOLDER_PREFIXES)
        # Broken output (empty, errored, sentinel model, or a placeholder body)
        # counts as EMPTY — the model failed. Only a real, substantive draft
        # produced by something other than the local LoRA is a cloud fallback.
        if not body or err or model.lower() in _BROKEN_MODELS or is_placeholder:
            empty += 1
        elif "lora" not in model.lower():
            cloud += 1
    fallback_rate = round(cloud / total, 4) if total else 0.0
    empty_rate = round(empty / total, 4) if total else 0.0
    judged = total >= min_drafts
    return {
        "total": total,
        "cloud_fallbacks": cloud,
        "empties": empty,
        "fallback_rate": fallback_rate,
        "empty_rate": empty_rate,
        "spike": {
            "fallback": judged and fallback_rate > fallback_spike,
            "empty": judged and empty_rate > empty_spike,
        },
    }
