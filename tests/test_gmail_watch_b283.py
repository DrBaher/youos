"""Gmail watch renewal (b283): keep real-time push (b282) alive past 7 days.

Pins the gog ``watch`` wrappers (command shape, JSON/non-JSON/failure/timeout)
and the nightly renewal step (gated, per-account, failure-isolated).
"""

from __future__ import annotations

import subprocess

import pytest

from app.ingestion import gmail_watch as gw


class _CP:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_renew_watch_parses_json_and_command(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        gw.subprocess, "run",
        lambda cmd, **k: (seen.update(cmd=cmd) or _CP(0, '{"historyId":"42","expiration":"1799999999000"}')),
    )
    res = gw.renew_watch("me@x.com")
    assert res["historyId"] == "42"
    assert seen["cmd"][:4] == ["gog", "gmail", "watch", "renew"]
    assert "--account" in seen["cmd"] and "me@x.com" in seen["cmd"] and "--json" in seen["cmd"]


def test_start_watch_includes_topic(monkeypatch):
    seen = {}
    monkeypatch.setattr(gw.subprocess, "run", lambda cmd, **k: (seen.update(cmd=cmd) or _CP(0, "{}")))
    gw.start_watch("me@x.com", topic="projects/p/topics/t")
    assert seen["cmd"][:4] == ["gog", "gmail", "watch", "start"]
    assert "--topic" in seen["cmd"] and "projects/p/topics/t" in seen["cmd"]


def test_renew_watch_raises_on_failure(monkeypatch):
    monkeypatch.setattr(gw.subprocess, "run", lambda cmd, **k: _CP(1, "", "no watch configured"))
    with pytest.raises(ValueError, match="no watch configured"):
        gw.renew_watch("me@x.com")


def test_watch_timeout_raises(monkeypatch):
    def boom(cmd, **k):
        raise subprocess.TimeoutExpired(cmd, gw.WATCH_TIMEOUT_SECONDS)

    monkeypatch.setattr(gw.subprocess, "run", boom)
    with pytest.raises(ValueError, match="timed out"):
        gw.renew_watch("me@x.com")


def test_non_json_output_wrapped(monkeypatch):
    monkeypatch.setattr(gw.subprocess, "run", lambda cmd, **k: _CP(0, "renewed OK"))
    assert gw.watch_status("me@x.com") == {"raw": "renewed OK"}


# --- nightly step ----------------------------------------------------------


def test_step_skips_when_push_disabled(monkeypatch):
    from scripts import nightly_pipeline as np

    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {"agent": {"gmail_push": {"enabled": False}}})
    assert "skipped" in np.step_gmail_watch_renew()


def test_step_renews_each_account_when_enabled(monkeypatch):
    from scripts import nightly_pipeline as np

    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {"agent": {"gmail_push": {"enabled": True}}})
    monkeypatch.setattr(np, "ACCOUNTS", ["a@x.com", "b@x.com"])
    monkeypatch.setattr("app.ingestion.gmail_watch.renew_watch", lambda acct: {"expiration": "1"})
    assert np.step_gmail_watch_renew() == "renewed 2/2"


def test_step_isolates_per_account_failure(monkeypatch):
    from scripts import nightly_pipeline as np

    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {"agent": {"gmail_push": {"enabled": True}}})
    monkeypatch.setattr(np, "ACCOUNTS", ["a@x.com", "b@x.com"])

    def renew(acct):
        if acct == "b@x.com":
            raise ValueError("boom")
        return {"expiration": "1"}

    monkeypatch.setattr("app.ingestion.gmail_watch.renew_watch", renew)
    res = np.step_gmail_watch_renew()
    assert "renewed 1/2" in res and "b@x.com" in res
