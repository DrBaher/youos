"""Gmail watch lifecycle via the gog CLI (b283).

A Gmail ``users.watch`` registration — the source of the b282 real-time push —
EXPIRES after 7 days; if it lapses, Pub/Sub notifications silently stop. gog
exposes the full lifecycle (start / status / renew / stop), so YouOS keeps the
watch alive by running ``gog gmail watch renew`` from the nightly (daily,
comfortably inside the 7-day window). One-time ``start`` registers it against the
Pub/Sub topic; ``renew`` reuses gog's stored config.

Same subprocess transport as the rest of the gog backend: the binary is on PATH,
the env (incl. ``GOG_KEYRING_PASSWORD`` under launchd) is inherited.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

WATCH_TIMEOUT_SECONDS = 60


def _run_watch(subcmd: list[str], account: str) -> dict[str, Any]:
    """Run ``gog gmail watch <subcmd> --account <account> --json``; return parsed
    JSON (``{}`` if empty, ``{"raw": …}`` if non-JSON). Raises ValueError on a
    non-zero exit or timeout."""
    cmd = ["gog", "gmail", "watch", *subcmd, "--account", account, "--json", "--no-input"]
    try:
        completed = subprocess.run(
            cmd, check=False, capture_output=True, text=True, timeout=WATCH_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            f"gog gmail watch {' '.join(subcmd)} timed out after {WATCH_TIMEOUT_SECONDS}s"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown gog error").strip()
        raise ValueError(f"gog gmail watch {' '.join(subcmd)} failed: {detail}")
    out = (completed.stdout or "").strip()
    if not out:
        return {}
    try:
        parsed = json.loads(out)
        return parsed if isinstance(parsed, dict) else {"result": parsed}
    except json.JSONDecodeError:
        return {"raw": out}


def start_watch(account: str, *, topic: str) -> dict[str, Any]:
    """Register the Gmail watch against a Pub/Sub topic (one-time setup).

    ``topic`` is a full resource name: ``projects/<project>/topics/<topic>``.
    """
    return _run_watch(["start", "--topic", topic], account)


def renew_watch(account: str) -> dict[str, Any]:
    """Renew the watch using gog's stored config (run on a schedule). Returns the
    new ``{historyId, expiration}`` (expiration is a ms-epoch ~7 days out)."""
    return _run_watch(["renew"], account)


def watch_status(account: str) -> dict[str, Any]:
    """gog's stored watch state for the account (empty dict if none)."""
    return _run_watch(["status"], account)


def stop_watch(account: str) -> dict[str, Any]:
    """Stop the watch and clear gog's stored state."""
    return _run_watch(["stop"], account)
