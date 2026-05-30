"""Proactive webhook push: decision logic (actionable + changed + throttled)."""

from __future__ import annotations

from types import SimpleNamespace

from app.agent import scheduler


def _digest(*, pending=0, owed=0, awaiting=0, ids=(1,)):
    return SimpleNamespace(
        pending_count=pending, owed_count=owed, awaiting_count=awaiting,
        pending_preview=[{"id": i} for i in ids], triage_url="http://host:8901",
    )


def _patch(monkeypatch, digest, posted):
    monkeypatch.setattr("app.agent.digest.build_digest", lambda **kw: digest)
    monkeypatch.setattr("app.agent.digest.summary_line", lambda d: "SUMMARY")

    def _fake_post(url, payload, secret):
        posted.append(payload)
        return True

    monkeypatch.setattr(scheduler, "_post_webhook", _fake_post)


def _cfg(**over):
    base = {"notify_webhook_url": "http://hook", "notify_webhook_secret": "", "notify_min_interval_minutes": 10}
    base.update(over)
    return base


def test_pushes_when_actionable_and_changed(monkeypatch):
    posted: list = []
    _patch(monkeypatch, _digest(pending=2, ids=(3, 4)), posted)
    app = SimpleNamespace(state=SimpleNamespace())
    scheduler._maybe_push_webhook(app, "you@x.com", _cfg())
    assert len(posted) == 1
    assert posted[0]["summary"] == "SUMMARY"
    assert posted[0]["pending_count"] == 2


def test_no_push_when_inbox_quiet(monkeypatch):
    posted: list = []
    _patch(monkeypatch, _digest(pending=0, owed=0, awaiting=0), posted)
    app = SimpleNamespace(state=SimpleNamespace())
    scheduler._maybe_push_webhook(app, "you@x.com", _cfg())
    assert posted == []


def test_no_push_when_unchanged_within_interval(monkeypatch):
    posted: list = []
    _patch(monkeypatch, _digest(pending=2, ids=(3, 4)), posted)
    app = SimpleNamespace(state=SimpleNamespace())
    scheduler._maybe_push_webhook(app, "you@x.com", _cfg())
    scheduler._maybe_push_webhook(app, "you@x.com", _cfg())  # same state, within interval
    assert len(posted) == 1


def test_no_push_when_url_unset(monkeypatch):
    posted: list = []
    _patch(monkeypatch, _digest(pending=5), posted)
    app = SimpleNamespace(state=SimpleNamespace())
    scheduler._maybe_push_webhook(app, "you@x.com", _cfg(notify_webhook_url=""))
    assert posted == []


def test_webhook_url_allowed_blocks_ssrf():
    """b136: the user-set webhook URL can't be pointed at internal/metadata
    hosts (SSRF). IP literals keep this deterministic (no DNS)."""
    from app.agent.scheduler import _webhook_url_allowed

    for bad in (
        "http://127.0.0.1/x",                       # loopback
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.1.2.3/", "https://192.168.1.1/",  # RFC1918
        "file:///etc/passwd", "ftp://8.8.8.8/",      # non-http(s) scheme
    ):
        assert _webhook_url_allowed(bad) is False, bad
    assert _webhook_url_allowed("https://8.8.8.8/hook") is True  # public IP ok
