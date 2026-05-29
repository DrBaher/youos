"""Background scheduler for autonomous agent triage (γ).

Runs ``run_triage()`` periodically inside the running ``youos serve`` process
— started from the FastAPI lifespan, stopped cleanly on shutdown. Opt-in via
``agent.enabled``; off by default so installing YouOS doesn't quietly start
polling your inbox.

Safety:
- Never starts under pytest (``PYTEST_CURRENT_TEST`` env probe).
- Per-iteration failures are logged + swallowed — one bad sweep (transient
  gog auth failure, network blip) doesn't stop the loop.
- macOS notification fires only when persisted > 0 in that iteration, so a
  quiet inbox doesn't generate Notification Center spam.
- Config is re-read every iteration so flipping ``agent.enabled false``
  takes effect on the next tick (no restart needed).
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from functools import partial
from typing import Any

logger = logging.getLogger(__name__)

_STOP_EVENT_ATTR = "_agent_loop_stop"
_TASK_ATTR = "_agent_loop_task"
_FAILURES_ATTR = "_agent_sweep_failures"


def get_agent_config() -> dict[str, Any]:
    """Read ``agent.*`` settings from ``youos_config.yaml``. All keys have
    safe defaults so a missing section is fine."""
    from app.core.config import load_config

    cfg = load_config() or {}
    a = cfg.get("agent", {}) if isinstance(cfg, dict) else {}
    if not isinstance(a, dict):
        a = {}
    return {
        "enabled": bool(a.get("enabled", False)),
        "interval_minutes": max(1, int(a.get("interval_minutes", 15))),
        # b58: accept either a list (programmatic YAML edit) or a comma-
        # separated string (the textarea form set via ``youos config set
        # agent.accounts ...``). Empty falls back to ``user.emails`` in
        # ``_resolve_accounts`` so single-account setups don't need to
        # touch this flag at all.
        "accounts": _parse_skip_senders(a.get("accounts")),
        "window": str(a.get("window", "24h")),
        "limit": int(a.get("limit", 25)),
        "threshold": float(a.get("threshold", 0.6)),
        "notify_macos": bool(a.get("notify_macos", True)),
        # δ: free-form text prepended to every triage draft's prompt — e.g.
        # "today I'm out of office; politely decline meetings." Stored as
        # ``agent.standing_instructions`` so it's editable from /settings,
        # /triage, or a single ``youos config set`` line.
        "standing_instructions": str(a.get("standing_instructions") or "").strip(),
        # ζ: safety guardrails. ``skip_senders`` is a comma-separated string
        # for textarea-friendliness; parsed into a normalised list at use
        # time. ``daily_draft_cap`` is per-UTC-day per-account; 0 = unlimited.
        # ``strict_local`` refuses cloud fallback during triage only.
        "skip_senders": _parse_skip_senders(a.get("skip_senders")),
        "vip_senders": _parse_skip_senders(a.get("vip_senders")),
        "daily_draft_cap": max(0, int(a.get("daily_draft_cap", 50))),
        "strict_local": bool(a.get("strict_local", False)),
    }


def _parse_skip_senders(raw: Any) -> list[str]:
    """Normalise the ``agent.skip_senders`` textarea value to a deduped list
    of lowercase entries. Accepts the string form (comma-separated) for
    user convenience and the list form for programmatic config edits."""
    if not raw:
        return []
    if isinstance(raw, str):
        items = [s.strip() for s in raw.replace("\n", ",").split(",")]
    elif isinstance(raw, (list, tuple)):
        items = [str(s).strip() for s in raw]
    else:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for s in items:
        s = s.lower()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _notify_macos(*, title: str, message: str) -> None:
    """Best-effort macOS notification via ``osascript``. Silently no-ops on
    non-Darwin or if the call fails — agent uptime > notification fidelity."""
    try:
        # Quote-escape title + message so the AppleScript string literal stays valid
        # for ASCII-y payloads. Truncate aggressively (Notification Center truncates
        # at ~120 chars anyway).
        t = title.replace('"', "'")[:60]
        m = message.replace('"', "'")[:160]
        subprocess.run(
            ["osascript", "-e", f'display notification "{m}" with title "{t}"'],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        # Don't let notification failures break the agent loop.
        pass


def _resolve_accounts(configured: list[str]) -> list[str]:
    """Use the explicit ``agent.accounts`` list if set; otherwise fall back
    to ``user.emails``. Returns an empty list if neither is configured."""
    if configured:
        return [a for a in configured if a]
    try:
        from app.core.config import get_user_emails

        return [e for e in (get_user_emails() or []) if e]
    except Exception:
        return []


def _run_one_sweep(account: str, cfg: dict[str, Any]) -> int:
    """Run one triage sweep against one account. Returns the new-rows count
    (``persisted`` from the orchestrator), which is what the notification
    threshold cares about."""
    from app.agent.triage import run_triage
    from app.core.settings import get_settings

    s = get_settings()
    result = run_triage(
        account=account,
        window=cfg["window"],
        limit=cfg["limit"],
        threshold=cfg["threshold"],
        database_url=s.database_url,
        configs_dir=s.configs_dir,
        trigger="scheduled",
    )
    return result.persisted


async def _loop(app) -> None:
    """The agent loop body. Sleeps via the ``stop`` event so shutdown is
    immediate — it doesn't have to wait the full ``interval_minutes`` for
    the next tick before noticing the server is going down."""
    while True:
        cfg = get_agent_config()
        if cfg["enabled"]:
            accounts = _resolve_accounts(cfg["accounts"])
            total_persisted = 0
            # Per-account consecutive-failure tracking persists across ticks so
            # a silently-dying agent gets exactly one notification on the first
            # failure (debounced), not spam every tick and not silence.
            failures: dict[str, int] = getattr(app.state, _FAILURES_ATTR, {})
            for account in accounts:
                try:
                    n = await asyncio.get_event_loop().run_in_executor(
                        None, partial(_run_one_sweep, account, cfg)
                    )
                    total_persisted += int(n)
                    # Recovery: announce once if it had been failing.
                    if failures.get(account, 0) > 0 and cfg["notify_macos"]:
                        _notify_macos(
                            title="YouOS",
                            message=f"Agent recovered — sweeps for {account} are working again.",
                        )
                    failures[account] = 0
                except Exception as exc:
                    prev = failures.get(account, 0)
                    failures[account] = prev + 1
                    # First failure → warn loudly + notify so a dead agent is
                    # visible. Subsequent consecutive failures stay quiet.
                    if prev == 0:
                        logger.warning("agent loop: sweep for %s failed: %s", account, exc)
                        if cfg["notify_macos"]:
                            _notify_macos(
                                title="YouOS agent stopped drafting",
                                message=f"Sweep for {account} failed: {exc}. Check youos doctor.",
                            )
                    else:
                        logger.warning(
                            "agent loop: sweep for %s still failing (%dx): %s",
                            account, prev + 1, exc,
                        )
            setattr(app.state, _FAILURES_ATTR, failures)

            if total_persisted > 0 and cfg["notify_macos"]:
                s = "s" if total_persisted != 1 else ""
                _notify_macos(
                    title="YouOS",
                    message=f"{total_persisted} new draft{s} ready in /triage",
                )

        # Wait for either the configured interval or a shutdown signal.
        stop: asyncio.Event | None = getattr(app.state, _STOP_EVENT_ATTR, None)
        if stop is None:
            return
        # 60s minimum interval — guardrail against accidental tight-loop config.
        interval = max(60, cfg["interval_minutes"] * 60)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return  # shutdown
        except asyncio.TimeoutError:
            continue  # next tick


def start(app) -> None:
    """Start the background loop. Called from the FastAPI lifespan startup.
    No-op under pytest (so tests don't get a stray background task)."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    if getattr(app.state, _TASK_ATTR, None) is not None:
        return  # already running
    app.state._agent_loop_stop = asyncio.Event()
    app.state._agent_loop_task = asyncio.create_task(_loop(app))
    logger.info("agent scheduler: background loop started")


async def stop(app) -> None:
    """Signal the loop to exit and await it. Called from the lifespan
    shutdown side; cancels if the loop doesn't clean up in 5s."""
    ev: asyncio.Event | None = getattr(app.state, _STOP_EVENT_ATTR, None)
    task: asyncio.Task | None = getattr(app.state, _TASK_ATTR, None)
    if ev is None or task is None:
        return
    ev.set()
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()
    finally:
        app.state._agent_loop_stop = None
        app.state._agent_loop_task = None
