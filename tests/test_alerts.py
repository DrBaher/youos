"""Tests for proactive alerting + failure classification (Phase C)."""

from __future__ import annotations

from dataclasses import dataclass

from app.agent.alerts import classify_sweep_failure, sweep_health

# --- classify_sweep_failure ------------------------------------------------


def test_classify_auth_failure():
    fc = classify_sweep_failure("gog gmail search failed: No auth for gmail you@x.com.")
    assert fc.kind == "auth"
    assert "gog auth login" in fc.message


def test_classify_rate_limit():
    assert classify_sweep_failure("HTTP 429 Too Many Requests").kind == "rate_limit"
    assert classify_sweep_failure("quota exceeded").kind == "rate_limit"


def test_classify_network():
    assert classify_sweep_failure("connection timed out").kind == "network"
    assert classify_sweep_failure("getaddrinfo failed").kind == "network"


def test_classify_unknown():
    fc = classify_sweep_failure("KeyError: 'foo'")
    assert fc.kind == "unknown"
    assert "doctor" in fc.message


def test_classify_handles_none():
    assert classify_sweep_failure(None).kind == "unknown"


# --- sweep_health ----------------------------------------------------------


@dataclass
class _Draft:
    model_used: str | None = "qwen2.5-1.5b-lora"
    draft: str | None = "Hello, this is a fine reply."
    error: str | None = None


def test_healthy_sweep_no_spike():
    drafts = [_Draft() for _ in range(5)]
    h = sweep_health(drafts)
    assert h["cloud_fallbacks"] == 0
    assert h["empties"] == 0
    assert h["spike"]["fallback"] is False
    assert h["spike"]["empty"] is False


def test_cloud_fallback_spike():
    drafts = [_Draft(model_used="claude-cloud") for _ in range(4)] + [_Draft()]
    h = sweep_health(drafts)
    assert h["cloud_fallbacks"] == 4
    assert h["fallback_rate"] == 0.8
    assert h["spike"]["fallback"] is True


def test_empty_output_spike():
    drafts = [_Draft(draft="") for _ in range(3)] + [_Draft(error="boom", draft=None)] + [_Draft()]
    h = sweep_health(drafts)
    assert h["empties"] == 4
    assert h["spike"]["empty"] is True


def test_no_spike_below_min_drafts():
    # Two cloud drafts = 100% fallback, but too few to judge → no spike.
    drafts = [_Draft(model_used="claude-cloud"), _Draft(model_used="claude-cloud")]
    h = sweep_health(drafts, min_drafts=3)
    assert h["fallback_rate"] == 1.0
    assert h["spike"]["fallback"] is False


def test_empty_takes_precedence_over_fallback_classification():
    # An empty cloud draft counts as empty, not as a fallback.
    drafts = [_Draft(model_used="claude-cloud", draft="") for _ in range(3)]
    h = sweep_health(drafts)
    assert h["empties"] == 3
    assert h["cloud_fallbacks"] == 0
