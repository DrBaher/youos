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
  takes effect on the next tick (no restart needed). This holds even for an
  edit made by ANOTHER process (the nightly auto-tuning ``agent.threshold``,
  a ``youos config set`` CLI): ``get_agent_config`` calls
  ``reload_config_if_changed``, which drops the in-process ``load_config``
  cache when the file's mtime changes — without it the server would pin the
  stale value until restart.
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
_WEBHOOK_ATTR = "_agent_webhook_state"


def _safe_int(value: Any, default: int) -> int:
    """``int(value)`` that degrades to ``default`` on a non-numeric value.
    Guards the config read against a poisoned int flag (e.g. a hand-edited YAML
    or a value persisted before coerce_value validated it) — a bad value must
    never raise out of get_agent_config and kill the agent loop."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float) -> float:
    """``float(value)`` that degrades to ``default`` on a non-numeric value."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_agent_config() -> dict[str, Any]:
    """Read ``agent.*`` settings from ``youos_config.yaml``. All keys have
    safe defaults so a missing section is fine. Numeric reads degrade to their
    default on a malformed value so a poisoned flag can't crash the loop.

    Honours config edits made by OTHER processes (the nightly's threshold
    auto-tune, a ``youos config set`` CLI) via ``reload_config_if_changed`` —
    without it the server's in-process ``load_config`` cache would pin stale
    values until a restart, so a nightly-tuned threshold would never take
    effect on the running scheduler."""
    from app.core.config import load_config, reload_config_if_changed

    reload_config_if_changed()
    cfg = load_config() or {}
    a = cfg.get("agent", {}) if isinstance(cfg, dict) else {}
    if not isinstance(a, dict):
        a = {}
    return {
        "enabled": bool(a.get("enabled", False)),
        "interval_minutes": max(1, _safe_int(a.get("interval_minutes", 15), 15)),
        # b58: accept either a list (programmatic YAML edit) or a comma-
        # separated string (the textarea form set via ``youos config set
        # agent.accounts ...``). Empty falls back to ``user.emails`` in
        # ``_resolve_accounts`` so single-account setups don't need to
        # touch this flag at all.
        "accounts": _parse_skip_senders(a.get("accounts")),
        "window": str(a.get("window", "24h")),
        "limit": _safe_int(a.get("limit", 25), 25),
        "threshold": _safe_float(a.get("threshold", 0.6), 0.6),
        # When True (default), the nightly auto-tunes ``threshold`` from real
        # send outcomes (sent vs no_send) — raising it when most queued drafts
        # go unanswered, lowering it when almost all earn a reply. See
        # app.agent.threshold_tuner.
        "auto_tune_threshold": bool(a.get("auto_tune_threshold", True)),
        "notify_macos": bool(a.get("notify_macos", True)),
        # Minimum spacing between on-demand /api/agent/triage sweeps for one
        # account. A sweep is a full fetch+filter+draft cycle (~30-60s, costs
        # gog auth + model time), so an orchestrator that loops "triage again"
        # would hammer Gmail and the model server. The API rejects a sweep
        # requested within this window with 429 + Retry-After. The background
        # scheduler is unaffected (it paces itself via interval_minutes). 0
        # disables the guard.
        "triage_min_interval_seconds": max(0, _safe_int(a.get("triage_min_interval_seconds", 60), 60)),
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
        "daily_draft_cap": max(0, _safe_int(a.get("daily_draft_cap", 50), 50)),
        "strict_local": bool(a.get("strict_local", False)),
        # Proactive push: POST a digest summary to a user-configured webhook
        # after a sweep so an absent user (or their Telegram/OpenClaw bot) is
        # nudged without polling. Off unless a URL is set — the one place YouOS
        # makes an outbound request, metadata-only.
        "notify_webhook_url": str(a.get("notify_webhook_url") or "").strip(),
        "notify_webhook_secret": str(a.get("notify_webhook_secret") or "").strip(),
        "notify_min_interval_minutes": max(0, _safe_int(a.get("notify_min_interval_minutes", 10), 10)),
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
        # Pass title + message as osascript argv (NOT interpolated into the
        # AppleScript source), so a backslash / quote / control char in the text
        # can't make the script a syntax error and silently drop the alert —
        # which would hide exactly the agent-died failure this notification
        # exists to surface. Truncate (Notification Center truncates ~120 anyway).
        subprocess.run(
            [
                "osascript",
                "-e", "on run argv",
                "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
                "-e", "end run",
                message[:200],
                title[:80],
            ],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        # Don't let notification failures break the agent loop.
        pass


def _webhook_url_allowed(url: str) -> bool:
    """Guard the (user-configured) webhook URL against SSRF: require an http(s)
    scheme and refuse a host that resolves to a loopback / private / link-local /
    reserved address, so the one outbound request YouOS makes can't be pointed at
    a cloud metadata endpoint or an internal service. Fails closed."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        infos = socket.getaddrinfo(parsed.hostname, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError, ValueError):
        return False  # unresolvable → don't send
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def _post_webhook(url: str, payload: dict[str, Any], secret: str) -> bool:
    """POST a JSON payload to ``url``. Best-effort, bounded, never raises.

    The ``secret`` (if set) goes in an ``X-YouOS-Secret`` header so the
    receiver can verify it's really YouOS. Returns True on a 2xx response."""
    import json as _json
    import urllib.request

    if not _webhook_url_allowed(url):
        logger.warning("agent webhook push refused: %s is not an allowed external URL", url)
        return False
    try:
        body = _json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if secret:
            req.add_header("X-YouOS-Secret", secret)
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 — validated external URL
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception as exc:
        logger.info("agent webhook push failed: %s", exc)
        return False


# Per-(kind, account) last-alert monotonic timestamp, so a recurring condition
# (auth expired every tick) alerts once per debounce window, not every sweep.
_LAST_ALERT_TS: dict[str, float] = {}


def _alert(cfg: dict[str, Any], *, kind: str, account: str, title: str, message: str) -> bool:
    """Fire a proactive alert on every configured channel (macOS + webhook),
    debounced per (kind, account). Returns True if it actually fired.

    This is the "not just a WARN in a log" path — a dead/degraded agent reaches
    the user. Best-effort; never raises."""
    import time

    key = f"{kind}:{account}"
    interval_s = max(0, int(cfg.get("notify_min_interval_minutes", 10))) * 60
    now = time.monotonic()
    last = _LAST_ALERT_TS.get(key)
    if last is not None and interval_s and (now - last) < interval_s:
        return False
    _LAST_ALERT_TS[key] = now
    fired = False
    if cfg.get("notify_macos"):
        _notify_macos(title=title, message=message)
        fired = True
    url = cfg.get("notify_webhook_url") or ""
    if url:
        _post_webhook(
            url,
            {"type": "alert", "kind": kind, "account": account, "title": title, "message": message},
            cfg.get("notify_webhook_secret") or "",
        )
        fired = True
    return fired


def _maybe_push_webhook(app, account: str, cfg: dict[str, Any]) -> None:
    """After a sweep, push a digest summary to the configured webhook — but only
    when there's something actionable, the state CHANGED since the last push,
    and the min-interval has elapsed (so a quiet or unchanged inbox stays
    quiet). Metadata-only: counts + truncated subjects/senders, never bodies."""
    import time as _time

    url = cfg.get("notify_webhook_url") or ""
    if not url:
        return
    try:
        from app.agent.digest import build_digest, summary_line
        from app.core.settings import get_settings

        data = build_digest(database_url=get_settings().database_url, account=account, days=1)
    except Exception as exc:
        logger.info("agent webhook: digest build failed: %s", exc)
        return

    # Nothing worth interrupting the user for.
    if data.pending_count == 0 and data.owed_count == 0 and data.awaiting_count == 0:
        return

    sig = (
        f"{data.pending_count}:{data.owed_count}:{data.awaiting_count}:"
        + ",".join(str(r.get("id")) for r in data.pending_preview)
    )
    state: dict[str, dict[str, Any]] = getattr(app.state, _WEBHOOK_ATTR, {})
    prev = state.get(account, {})
    now = _time.monotonic()
    min_interval = cfg.get("notify_min_interval_minutes", 10) * 60

    last_ts = prev.get("ts")
    # Throttle only relative to a PRIOR push. ``monotonic()`` is seconds since an
    # arbitrary point (often small on a freshly-booted host), so comparing
    # against a 0.0 default would wrongly throttle the very first push.
    if last_ts is not None and (now - last_ts) < min_interval:
        return  # throttle — don't flap
    if prev.get("sig") == sig:
        return  # nothing changed since the last push

    payload = {
        "summary": summary_line(data),
        "account": account,
        "pending_count": data.pending_count,
        "owed_count": data.owed_count,
        "awaiting_count": data.awaiting_count,
        # pending_preview already carries only id/tier/score/sender/truncated
        # subject — no message bodies.
        "pending_preview": data.pending_preview,
        "triage_url": data.triage_url,
    }
    if _post_webhook(url, payload, cfg.get("notify_webhook_secret") or ""):
        state[account] = {"ts": now, "sig": sig}
        setattr(app.state, _WEBHOOK_ATTR, state)


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
    # Sweep-health alarm: a "successful" sweep is still unhealthy if most drafts
    # fell back to the cloud (local model down) or came back empty. Alert on a
    # spike — these are silent-degradation modes the persisted-count never shows.
    try:
        from app.agent.alerts import sweep_health

        health = sweep_health(result.drafts)
        if health["spike"]["fallback"]:
            _alert(
                cfg, kind="fallback_spike", account=account,
                title="YouOS agent: local model may be down",
                message=(
                    f"{health['cloud_fallbacks']}/{health['total']} drafts used the cloud "
                    f"fallback this sweep — the local model server may be down."
                ),
            )
        elif health["spike"]["empty"]:
            _alert(
                cfg, kind="empty_spike", account=account,
                title="YouOS agent: drafts coming back empty",
                message=(
                    f"{health['empties']}/{health['total']} drafts were empty this sweep — "
                    f"the model/adapter may be broken. Check `youos doctor`."
                ),
            )
    except Exception as exc:
        logger.info("sweep-health check skipped: %s", exc)
    return result.persisted


async def _run_tick(app) -> int:
    """One scheduler tick: read config, sweep each enabled account, run due
    digests, notify. Returns the seconds to wait before the next tick. May
    raise — _loop guards it so a bad tick can never kill the loop."""
    cfg = get_agent_config()
    # 60s minimum interval — guardrail against accidental tight-loop config.
    interval = max(60, cfg["interval_minutes"] * 60)
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
                # Recovery: announce once on every channel (not just macOS)
                # if it had been failing, and clear the failure debounce so a
                # later re-failure isn't wrongly suppressed.
                if failures.get(account, 0) > 0:
                    for k in [k for k in _LAST_ALERT_TS if k.endswith(f":{account}") and k.startswith("sweep_fail:")]:
                        _LAST_ALERT_TS.pop(k, None)
                    _alert(
                        cfg, kind="recovered", account=account,
                        title="YouOS",
                        message=f"Agent recovered — sweeps for {account} are working again.",
                    )
                failures[account] = 0
                # Proactive push (opt-in, off unless a webhook URL is set).
                if cfg.get("notify_webhook_url"):
                    try:
                        await asyncio.get_event_loop().run_in_executor(
                            None, partial(_maybe_push_webhook, app, account, cfg)
                        )
                    except Exception as exc:
                        logger.info("agent webhook push errored: %s", exc)
            except Exception as exc:
                prev = failures.get(account, 0)
                failures[account] = prev + 1
                # Classify the failure into an actionable alert (auth /
                # rate-limit / network / unknown) and fire on every channel
                # — not just a log line. _alert debounces per (kind,
                # account), so a recurring cause (expired auth every tick)
                # alerts once per window rather than spamming.
                from app.agent.alerts import classify_sweep_failure

                fc = classify_sweep_failure(str(exc))
                logger.warning(
                    "agent loop: sweep for %s failed (%s, %dx): %s",
                    account, fc.kind, prev + 1, exc,
                )
                _alert(cfg, kind=f"sweep_fail:{fc.kind}", account=account,
                       title=fc.title, message=fc.message)
        setattr(app.state, _FAILURES_ATTR, failures)

        # Scheduled digest tasks (collect → summarize → send one digest).
        # No-op unless agent.digests.enabled; the per-period claim makes
        # repeated ticks idempotent, so checking every tick is safe.
        # Failure-isolated — a digest error never disrupts the sweep loop.
        try:
            from app.agent.digest_tasks import run_due_digests
            from app.core.settings import get_settings

            db_url = get_settings().database_url
            for account in accounts:
                await asyncio.get_event_loop().run_in_executor(
                    None, partial(run_due_digests, db_url, account)
                )
        except Exception as exc:
            logger.info("digest tasks step errored: %s", exc)

        if total_persisted > 0 and cfg["notify_macos"]:
            s = "s" if total_persisted != 1 else ""
            _notify_macos(
                title="YouOS",
                message=f"{total_persisted} new draft{s} ready in /triage",
            )
    return interval


async def _loop(app) -> None:
    """The agent loop body. Sleeps via the ``stop`` event so shutdown is
    immediate — it doesn't have to wait the full ``interval_minutes`` for
    the next tick before noticing the server is going down.

    Each tick runs inside a guard: an unexpected error (a transient backend
    failure, a poisoned config) is logged and the loop waits out the interval
    and retries — so a single bad tick can never kill this fire-and-forget
    task, which start() cannot restart once it has died."""
    while True:
        try:
            interval = await _run_tick(app)
        except Exception:
            logger.exception("agent loop: tick failed; retrying next interval")
            interval = 15 * 60
        # Wait for either the configured interval or a shutdown signal.
        stop: asyncio.Event | None = getattr(app.state, _STOP_EVENT_ATTR, None)
        if stop is None:
            return
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
