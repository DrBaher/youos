"""γ: background scheduler — config, loop body, notification gate.

Tests exercise the loop with a stubbed sweep + stop event so we don't need
real intervals or a real Gmail backend.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


# --- config reader ---------------------------------------------------------


def test_get_agent_config_defaults(monkeypatch):
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {})
    from app.agent.scheduler import get_agent_config

    cfg = get_agent_config()
    assert cfg["enabled"] is False
    assert cfg["interval_minutes"] == 15
    assert cfg["accounts"] == []
    assert cfg["window"] == "24h"
    assert cfg["limit"] == 25
    assert cfg["threshold"] == 0.6
    assert cfg["notify_macos"] is True


def test_get_agent_config_overrides(monkeypatch):
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **k: {
            "agent": {
                "enabled": True,
                "interval_minutes": 5,
                "accounts": ["a@x.com", "b@y.com"],
                "window": "3d",
                "limit": 50,
                "threshold": 0.5,
                "notify_macos": False,
            }
        },
    )
    from app.agent.scheduler import get_agent_config

    cfg = get_agent_config()
    assert cfg["enabled"] is True
    assert cfg["interval_minutes"] == 5
    assert cfg["accounts"] == ["a@x.com", "b@y.com"]
    assert cfg["notify_macos"] is False


def test_get_agent_config_clamps_interval_floor(monkeypatch):
    """A user setting interval_minutes=0 must clamp to 1 (and the loop
    enforces a further 60-second floor at run time)."""
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **k: {"agent": {"interval_minutes": 0}},
    )
    from app.agent.scheduler import get_agent_config

    assert get_agent_config()["interval_minutes"] == 1


# --- account resolution ----------------------------------------------------


def test_resolve_accounts_uses_explicit_list_when_set(monkeypatch):
    from app.agent.scheduler import _resolve_accounts

    monkeypatch.setattr("app.core.config.get_user_emails", lambda *a, **k: ["fallback@x.com"])
    assert _resolve_accounts(["a@x.com", "b@y.com"]) == ["a@x.com", "b@y.com"]


def test_resolve_accounts_falls_back_to_user_emails_when_empty(monkeypatch):
    from app.agent.scheduler import _resolve_accounts

    monkeypatch.setattr("app.core.config.get_user_emails", lambda *a, **k: ("baher@medicus.ai", "drbaher@gmail.com"))
    assert _resolve_accounts([]) == ["baher@medicus.ai", "drbaher@gmail.com"]


# --- macOS notification ----------------------------------------------------


def test_notify_macos_swallows_subprocess_failure(monkeypatch):
    """An ``osascript`` failure (non-Darwin host, missing binary) must not
    bubble — agent uptime > notification fidelity."""
    import subprocess as _sub
    from app.agent import scheduler

    def _boom(*a, **k):
        raise FileNotFoundError("osascript: not found")

    monkeypatch.setattr(_sub, "run", _boom)
    # Must NOT raise.
    scheduler._notify_macos(title="x", message="y")


# --- loop behaviour --------------------------------------------------------


def test_loop_exits_immediately_when_disabled(monkeypatch):
    """A disabled agent loop must not call run_triage at all, and must exit
    cleanly when the stop event fires."""
    from app.agent import scheduler

    monkeypatch.setattr(scheduler, "get_agent_config", lambda: {
        "enabled": False, "interval_minutes": 15, "accounts": [],
        "window": "24h", "limit": 25, "threshold": 0.6, "notify_macos": True,
    })
    swept: list[str] = []
    monkeypatch.setattr(scheduler, "_run_one_sweep", lambda acct, cfg: swept.append(acct) or 0)

    app = SimpleNamespace(state=SimpleNamespace())
    app.state._agent_loop_stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.05)
        app.state._agent_loop_stop.set()

    async def _run():
        await asyncio.gather(scheduler._loop(app), _stop_soon())
    asyncio.run(_run())
    assert swept == []  # never swept


def test_loop_sweeps_each_configured_account_and_notifies_on_new_drafts(monkeypatch):
    """One enabled iteration: each account gets swept, total persisted is
    summed, notification fires once with the count when > 0."""
    from app.agent import scheduler

    monkeypatch.setattr(scheduler, "get_agent_config", lambda: {
        "enabled": True, "interval_minutes": 15,
        "accounts": ["a@x.com", "b@y.com"],
        "window": "24h", "limit": 25, "threshold": 0.6, "notify_macos": True,
    })
    monkeypatch.setattr(scheduler, "_resolve_accounts", lambda cfg_list: list(cfg_list))

    sweeps: list[str] = []
    def _fake_sweep(account, cfg):
        sweeps.append(account)
        return 1 if account == "a@x.com" else 2  # totals 3
    monkeypatch.setattr(scheduler, "_run_one_sweep", _fake_sweep)

    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(scheduler, "_notify_macos", lambda *, title, message: notifications.append((title, message)))

    app = SimpleNamespace(state=SimpleNamespace())
    app.state._agent_loop_stop = asyncio.Event()

    # Stop right after the first sweep finishes (before the long interval sleep).
    async def _stop_after_first_pass():
        await asyncio.sleep(0.1)
        app.state._agent_loop_stop.set()

    async def _run():
        await asyncio.gather(scheduler._loop(app), _stop_after_first_pass())
    asyncio.run(_run())

    assert sweeps == ["a@x.com", "b@y.com"]
    assert len(notifications) == 1
    assert "3 new drafts" in notifications[0][1]


def test_loop_does_not_notify_when_persisted_is_zero(monkeypatch):
    """A quiet sweep (no new drafts) must NOT notify."""
    from app.agent import scheduler

    monkeypatch.setattr(scheduler, "get_agent_config", lambda: {
        "enabled": True, "interval_minutes": 15, "accounts": ["a@x.com"],
        "window": "24h", "limit": 25, "threshold": 0.6, "notify_macos": True,
    })
    monkeypatch.setattr(scheduler, "_resolve_accounts", lambda cfg_list: ["a@x.com"])
    monkeypatch.setattr(scheduler, "_run_one_sweep", lambda acct, cfg: 0)

    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(scheduler, "_notify_macos", lambda **kw: notifications.append(("x", "x")))

    app = SimpleNamespace(state=SimpleNamespace())
    app.state._agent_loop_stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.1)
        app.state._agent_loop_stop.set()

    async def _run():
        await asyncio.gather(scheduler._loop(app), _stop_soon())
    asyncio.run(_run())
    assert notifications == []


def test_loop_swallows_sweep_failure_and_keeps_running(monkeypatch):
    """One account raising must not kill the loop or skip the others."""
    from app.agent import scheduler

    monkeypatch.setattr(scheduler, "get_agent_config", lambda: {
        "enabled": True, "interval_minutes": 15,
        "accounts": ["a@x.com", "b@y.com"],
        "window": "24h", "limit": 25, "threshold": 0.6, "notify_macos": True,
    })
    monkeypatch.setattr(scheduler, "_resolve_accounts", lambda cfg_list: list(cfg_list))

    swept_ok: list[str] = []
    def _flaky_sweep(account, cfg):
        if account == "a@x.com":
            raise RuntimeError("gog auth failed")
        swept_ok.append(account)
        return 0
    monkeypatch.setattr(scheduler, "_run_one_sweep", _flaky_sweep)
    monkeypatch.setattr(scheduler, "_notify_macos", lambda **kw: None)

    app = SimpleNamespace(state=SimpleNamespace())
    app.state._agent_loop_stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.1)
        app.state._agent_loop_stop.set()

    async def _run():
        await asyncio.gather(scheduler._loop(app), _stop_soon())
    asyncio.run(_run())
    # Second account still ran despite first one raising.
    assert swept_ok == ["b@y.com"]


# --- start/stop never spawn under pytest -----------------------------------


def test_start_is_a_noop_under_pytest():
    """``start()`` checks ``PYTEST_CURRENT_TEST`` and refuses to spawn — so
    tests can't accidentally launch a real background sweep."""
    from app.agent import scheduler

    app = SimpleNamespace(state=SimpleNamespace())
    scheduler.start(app)
    assert getattr(app.state, "_agent_loop_task", None) is None


# --- ζ: skip-list parsing --------------------------------------------------


def test_parse_skip_senders_comma_separated_string():
    from app.agent.scheduler import _parse_skip_senders

    assert _parse_skip_senders("alice@x.com, @bigcorp.com,bob@y.com ,") == [
        "alice@x.com", "@bigcorp.com", "bob@y.com",
    ]


def test_parse_skip_senders_list_form_and_dedup_lowercase():
    from app.agent.scheduler import _parse_skip_senders

    assert _parse_skip_senders(["Alice@X.com", "alice@x.com", "@BIGCORP.com"]) == [
        "alice@x.com", "@bigcorp.com",
    ]


def test_parse_skip_senders_empty_inputs():
    from app.agent.scheduler import _parse_skip_senders

    assert _parse_skip_senders(None) == []
    assert _parse_skip_senders("") == []
    assert _parse_skip_senders([]) == []
