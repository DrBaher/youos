"""b175: cloud (Claude) drafting opt-in escape hatch, hard-gated OFF.

Verifies the centralized, fail-closed cloud gate in ``app.generation.service``
— ``_cloud_escalation_allowed`` and its wiring into ``generate_draft``'s inline
backend selection:

  (a) flag OFF  -> never uses Claude even with allow_cloud_escalation=True
  (b) flag ON + allow_cloud_escalation + interactive -> uses Claude,
      model_used marked cloud + egress notice set
  (c) flag ON but a background/eval guard set
      (no_cloud_fallback / deterministic / strict_local)
      -> Claude HARD-BLOCKED (stays local, no egress)
  (d) flag ON but no explicit per-request opt-in -> local

Hermetic: no real mlx_lm / Claude CLI / model / network. We stub the full
retrieval / persona / persistence path of generate_draft (mirroring
tests/test_eval_determinism_b166.py) and patch the local + Claude seams to
record which backend ran. The never-send invariant is upstream and untouched.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import app.core.config as config
import app.generation.service as svc


@pytest.fixture(autouse=True)
def _clear_config_cache():
    """Isolate from the load_config lru_cache other tests pollute (e.g.
    test_config_write_api saves the real config). Without this, the full-suite
    run could see a written config and the cloud-escalation flag read drift."""
    config.load_config.cache_clear()
    yield
    config.load_config.cache_clear()


# --- shared stubbing (mirrors test_eval_determinism_b166) -------------------
def _stub_pipeline(monkeypatch):
    def _stub_retrieve(*a, **kw):
        return svc.RetrievalResponse(
            query="", retrieval_method="x", semantic_search_enabled=False,
            applied_filters={}, detected_mode=None, documents=[], chunks=[],
            reply_pairs=[],
        )

    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {})
    monkeypatch.setattr(svc, "retrieve_context", _stub_retrieve)
    monkeypatch.setattr(svc, "_load_prompts", lambda _d: {"system_prompt": "S"})
    monkeypatch.setattr(
        svc, "_load_persona",
        lambda _d: {"style": {"avg_reply_words": 30}, "modes": {}},
    )
    monkeypatch.setattr(svc, "lookup_sender_profile", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "lookup_facts", lambda **kw: [])
    monkeypatch.setattr(svc, "_lookup_prior_reply_to_sender", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "_adapter_available", lambda: False)
    monkeypatch.setattr(svc, "_persona_routing_enabled", lambda: False)
    monkeypatch.setattr(svc, "generate_subject", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "_log_draft_event", lambda *a, **kw: False)
    monkeypatch.setattr(
        svc, "_connect", lambda _p: __import__("sqlite3").connect(":memory:")
    )
    monkeypatch.setattr(svc, "resolve_sqlite_path", lambda _u: Path("/tmp/x.db"))
    monkeypatch.setattr(svc, "_resolve_decoding", lambda intent, conf: (None, None))
    monkeypatch.setattr(
        svc, "_multi_candidate_config",
        lambda: {"enabled": False, "temperatures": [0.3, 0.7, 1.0]},
    )
    # Default the config-derived levers to safe values so a polluted real config
    # can never leak a cloud fallback or the flag into these hermetic tests.
    # Individual tests override via _wire_backends(fallback=...) and _set_flag(...).
    monkeypatch.setattr(svc, "get_model_fallback", lambda *a, **k: "none")
    monkeypatch.setattr(svc, "cloud_escalation_enabled", lambda: False)


def _wire_backends(monkeypatch, *, local_available=True, fallback="none"):
    """Record which generation backend actually runs.

    The local model returns a usable draft; the Claude CLI records its
    invocation and returns a cloud draft. Tests assert on ``calls`` + the
    response markers.
    """
    calls: list[str] = []

    def fake_local_draft_once(messages, **kw):
        calls.append("local")
        return (
            "This is a perfectly good local draft, long enough to pass checks.",
            "qwen2.5-1.5b-base",
        )

    def fake_local(prompt, **kw):
        calls.append("local")
        return "This is a perfectly good local draft, long enough to pass checks."

    def fake_claude(prompt, *, max_tokens=300):
        calls.append("claude")
        return "This is a cloud (Claude) draft, long enough to pass the checks."

    monkeypatch.setattr(svc, "_local_model_available", lambda: local_available)
    monkeypatch.setattr(svc, "_local_draft_once", fake_local_draft_once)
    monkeypatch.setattr(svc, "_call_local_model", fake_local)
    monkeypatch.setattr(svc, "_call_claude_cli", fake_claude)
    monkeypatch.setattr(svc, "get_model_fallback", lambda *a, **k: fallback)
    return calls


def _set_flag(monkeypatch, enabled: bool):
    monkeypatch.setattr(svc, "cloud_escalation_enabled", lambda: enabled)


def _generate(req):
    return svc.generate_draft(req, database_url="sqlite:///x", configs_dir=Path("/tmp"))


def _req(**kw):
    return svc.DraftRequest(inbound_message="A hard case to reply to.", **kw)


# (a) flag OFF -> never cloud, even with explicit opt-in + cloud override -----
def test_flag_off_never_cloud_even_with_optin(monkeypatch):
    _stub_pipeline(monkeypatch)
    calls = _wire_backends(monkeypatch)
    _set_flag(monkeypatch, False)
    resp = _generate(
        _req(allow_cloud_escalation=True, backend_override="claude", use_local_model=True)
    )
    assert "claude" not in calls
    assert resp.cloud_used is False
    assert resp.egress_notice is None
    assert resp.model_used != "claude"


# (b) flag ON + opt-in + interactive -> cloud, marked + egress notice ---------
def test_flag_on_with_optin_uses_cloud(monkeypatch):
    _stub_pipeline(monkeypatch)
    calls = _wire_backends(monkeypatch)
    _set_flag(monkeypatch, True)
    # backend_override="claude" + use_local_model=False pins the cloud arm.
    resp = _generate(
        _req(allow_cloud_escalation=True, backend_override="claude",
             use_local_model=False)
    )
    assert "claude" in calls
    assert resp.model_used == "claude"
    assert resp.cloud_used is True
    assert resp.egress_notice is not None
    assert "cloud" in resp.egress_notice.lower()


def test_flag_on_optin_cloud_via_config_fallback(monkeypatch):
    # No override: config fallback is "claude" and local is unavailable, so the
    # cloud fallback runs — but only because the gate allows it (flag+opt-in).
    _stub_pipeline(monkeypatch)
    calls = _wire_backends(monkeypatch, local_available=False, fallback="claude")
    _set_flag(monkeypatch, True)
    resp = _generate(_req(allow_cloud_escalation=True))
    assert "claude" in calls
    assert resp.cloud_used is True


# (c) flag ON but a background/eval guard set -> HARD-BLOCKED -----------------
@pytest.mark.parametrize("guard", ["no_cloud_fallback", "deterministic", "strict_local"])
def test_flag_on_background_guard_hard_blocks_cloud(monkeypatch, guard):
    _stub_pipeline(monkeypatch)
    calls = _wire_backends(monkeypatch)
    _set_flag(monkeypatch, True)
    resp = _generate(
        _req(allow_cloud_escalation=True, backend_override="claude",
             use_local_model=False, **{guard: True})
    )
    assert "claude" not in calls, f"cloud reached despite {guard}"
    assert resp.cloud_used is False
    assert resp.egress_notice is None
    assert resp.model_used != "claude"


# (c2) even the config cloud-fallback is blocked on a background guard --------
@pytest.mark.parametrize("guard", ["no_cloud_fallback", "deterministic", "strict_local"])
def test_config_cloud_fallback_blocked_on_guard(monkeypatch, guard):
    _stub_pipeline(monkeypatch)
    calls = _wire_backends(monkeypatch, local_available=False, fallback="claude")
    _set_flag(monkeypatch, True)
    resp = _generate(_req(allow_cloud_escalation=True, **{guard: True}))
    assert "claude" not in calls, f"config cloud fallback reached despite {guard}"
    assert resp.cloud_used is False


# (c3) the gate predicate itself is fail-closed for each guard ---------------
@pytest.mark.parametrize("guard", ["no_cloud_fallback", "deterministic", "strict_local"])
def test_gate_predicate_denies_on_guard(monkeypatch, guard):
    _set_flag(monkeypatch, True)
    req = _req(allow_cloud_escalation=True, **{guard: True})
    assert svc._cloud_escalation_allowed(req) is False


# (d) flag ON but no explicit per-request opt-in -> local --------------------
def test_flag_on_without_optin_stays_local(monkeypatch):
    _stub_pipeline(monkeypatch)
    calls = _wire_backends(monkeypatch)
    _set_flag(monkeypatch, True)
    # backend_override="claude" but NO allow_cloud_escalation -> must stay local.
    resp = _generate(_req(backend_override="claude", use_local_model=True))
    assert "claude" not in calls
    assert resp.cloud_used is False
    assert resp.model_used != "claude"


def test_gate_predicate_denies_without_optin(monkeypatch):
    _set_flag(monkeypatch, True)
    assert svc._cloud_escalation_allowed(_req()) is False


def test_gate_predicate_allows_when_enabled_and_optin(monkeypatch):
    _set_flag(monkeypatch, True)
    assert svc._cloud_escalation_allowed(_req(allow_cloud_escalation=True)) is True


# Fail-closed: a config read error denies cloud -----------------------------
def test_config_error_fails_closed(monkeypatch):
    _stub_pipeline(monkeypatch)
    calls = _wire_backends(monkeypatch)

    def boom():
        raise RuntimeError("config blew up")

    monkeypatch.setattr(svc, "cloud_escalation_enabled", boom)
    resp = _generate(
        _req(allow_cloud_escalation=True, backend_override="claude",
             use_local_model=True)
    )
    assert "claude" not in calls
    assert resp.cloud_used is False


# Defaults: a plain DraftRequest never opts in (no background caller can leak).
def test_default_request_does_not_opt_in():
    assert svc.DraftRequest(inbound_message="x").allow_cloud_escalation is False


# DraftResponse carries the transparency fields (UI/telemetry can't hide cloud).
def test_response_exposes_cloud_fields():
    resp = svc.DraftResponse(
        draft="hi", detected_mode="x", precedent_used=[], retrieval_method="x",
        confidence="low", confidence_reason="x", model_used="qwen",
    )
    d = resp.to_dict()
    assert "cloud_used" in d
    assert "egress_notice" in d
    assert d["cloud_used"] is False
    assert d["egress_notice"] is None
