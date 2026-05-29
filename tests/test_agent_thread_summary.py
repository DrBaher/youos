"""Long-thread 'what changed' summaries."""

from __future__ import annotations

from app.agent import thread_summary


def _hist(n):
    return [{"sender": f"P{i} <p{i}@x.com>", "text": f"message body number {i} about the project"} for i in range(n)]


def test_short_thread_returns_none():
    # Below min_messages → no summary, no model call.
    assert thread_summary.summarize_thread(_hist(2), min_messages=4) is None
    assert thread_summary.summarize_thread(None, min_messages=4) is None


def test_disabled_model_returns_none(monkeypatch):
    monkeypatch.setattr("app.core.model_server.is_enabled", lambda: False)
    assert thread_summary.summarize_thread(_hist(6), min_messages=4) is None


def test_summarizes_long_thread_via_model(monkeypatch):
    seen = {}

    def _complete(prompt, **kw):
        seen["prompt"] = prompt
        return "  Pricing agreed at $X. Open: contract start date.  "

    monkeypatch.setattr("app.core.model_server.is_enabled", lambda: True)
    monkeypatch.setattr("app.core.model_server.complete", _complete)

    out = thread_summary.summarize_thread(_hist(6), subject="Q3 deal", min_messages=4)
    assert out == "Pricing agreed at $X. Open: contract start date."
    assert "Thread:" in seen["prompt"] and "Q3 deal" in seen["prompt"]


def test_model_failure_is_isolated(monkeypatch):
    monkeypatch.setattr("app.core.model_server.is_enabled", lambda: True)

    def _boom(*a, **k):
        raise RuntimeError("server down")

    monkeypatch.setattr("app.core.model_server.complete", _boom)
    assert thread_summary.summarize_thread(_hist(6), min_messages=4) is None
