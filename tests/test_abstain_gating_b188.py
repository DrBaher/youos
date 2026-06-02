"""Regression tests for b188: ABSTAIN on low-quality autonomous drafts.

``generate_draft`` historically ALWAYS returned a draft string for every
reply-worthy email, even when predicted quality was low — so weak throwaway
drafts ("Thanks, I'll check it out") surfaced as ready replies. b188 adds an
abstain path: when an AUTONOMOUS draft's quality_score is below the floor, the
response is marked ``withheld`` and the autonomous triage sweep routes the email
to the existing surface-for-review tier instead of presenting a weak draft.

The gate is tight and fail-closed — abstain fires ONLY when ALL hold:
    NOT request.deterministic  (eval/golden/autoresearch/nightly never abstain)
    AND NOT request.interactive (a human who asked to see a draft always gets one)
    AND generation.abstain.enabled
    AND quality_score < generation.abstain.min_quality

Hermetic: no real model, no network. We stub the retrieval/persona/persistence
path of generate_draft (mirroring tests/test_eval_determinism_b166.py) and stub
the per-draft quality score so we can drive the threshold directly.
"""
from __future__ import annotations

from pathlib import Path

import app.generation.service as svc

# --- shared stubbing (mirrors test_eval_determinism_b166) ------------------

def _stub_pipeline(monkeypatch, *, quality: float | None):
    """Stub everything generate_draft touches up to the model dispatch, and pin
    the per-draft quality score to ``quality``."""
    def _stub_retrieve(*a, **kw):
        return svc.RetrievalResponse(
            query="", retrieval_method="x", semantic_search_enabled=False,
            applied_filters={}, detected_mode=None, documents=[], chunks=[], reply_pairs=[],
        )

    monkeypatch.setattr(svc, "retrieve_context", _stub_retrieve)
    monkeypatch.setattr(svc, "_load_prompts", lambda _d: {"system_prompt": "S"})
    monkeypatch.setattr(svc, "_load_persona", lambda _d: {"style": {"avg_reply_words": 30}, "modes": {}})
    monkeypatch.setattr(svc, "lookup_sender_profile", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "lookup_facts", lambda **kw: [])
    monkeypatch.setattr(svc, "_lookup_prior_reply_to_sender", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "_local_model_available", lambda: True)
    monkeypatch.setattr(svc, "_adapter_available", lambda: False)
    monkeypatch.setattr(svc, "_persona_routing_enabled", lambda: False)
    monkeypatch.setattr(svc, "generate_subject", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "_log_draft_event", lambda *a, **kw: False)
    monkeypatch.setattr(svc, "_connect", lambda _p: __import__("sqlite3").connect(":memory:"))
    monkeypatch.setattr(svc, "resolve_sqlite_path", lambda _u: Path("/tmp/x.db"))
    monkeypatch.setattr(svc, "_resolve_decoding", lambda intent, conf: (None, None))
    monkeypatch.setattr(
        svc, "_multi_candidate_config",
        lambda: {"enabled": False, "temperatures": [0.3, 0.7, 1.0]},
    )
    # The model always returns a non-empty draft; quality is what we control.
    monkeypatch.setattr(
        svc, "_call_local_model",
        lambda prompt, **kw: "This is a drafted reply, long enough to pass the empty check.",
    )
    # Pin the per-draft quality score (the abstain threshold reads this).
    monkeypatch.setattr(svc, "draft_quality_score", lambda *a, **kw: quality)
    # Neutralize verify so it can't collapse the score under us.
    import app.generation.verify as _verify

    class _VR:
        issues: list[str] = []
        blocking = False
    monkeypatch.setattr(_verify, "verify_draft", lambda *a, **kw: _VR())


def _req(**kw):
    return svc.DraftRequest(inbound_message="Can we meet next week?", **kw)


def _generate(req):
    return svc.generate_draft(req, database_url="sqlite:///x", configs_dir=Path("/tmp"))


# --- (i) autonomous + below threshold -> WITHHELD --------------------------

def test_autonomous_low_quality_is_withheld(monkeypatch):
    _stub_pipeline(monkeypatch, quality=0.32)
    resp = _generate(_req(interactive=False, deterministic=False))
    assert resp.withheld is True, "autonomous low-quality draft must be withheld"
    assert resp.withhold_reason and "0.32" in resp.withhold_reason
    assert "0.50" in resp.withhold_reason
    # Work is NOT discarded: the draft text + score are kept for telemetry.
    assert isinstance(resp.draft, str) and resp.draft.strip()
    assert resp.quality_score == 0.32


# --- (ii) autonomous + above threshold -> NORMAL draft ---------------------

def test_autonomous_high_quality_not_withheld(monkeypatch):
    _stub_pipeline(monkeypatch, quality=0.80)
    resp = _generate(_req(interactive=False, deterministic=False))
    assert resp.withheld is False
    assert resp.withhold_reason is None
    assert isinstance(resp.draft, str) and resp.draft.strip()


# --- (iii) interactive (default) + low quality -> ALWAYS a draft -----------

def test_interactive_low_quality_still_returns_draft(monkeypatch):
    _stub_pipeline(monkeypatch, quality=0.10)
    # Default request is interactive=True -> never abstains.
    resp = _generate(_req())
    assert resp.withheld is False, "interactive draft must never be withheld"
    assert resp.withhold_reason is None
    assert isinstance(resp.draft, str) and resp.draft.strip()


def test_interactive_explicit_true_low_quality_still_returns_draft(monkeypatch):
    _stub_pipeline(monkeypatch, quality=0.10)
    resp = _generate(_req(interactive=True, deterministic=False))
    assert resp.withheld is False


# --- (iv) deterministic/eval path low quality -> NEVER abstains ------------

def test_deterministic_eval_never_abstains(monkeypatch):
    _stub_pipeline(monkeypatch, quality=0.05)
    # The golden/autoresearch/nightly family sets deterministic=True. Even with a
    # rock-bottom quality AND interactive=False, abstain must be inert there so
    # every eval case still drafts and scores -> golden eval byte-identical.
    resp = _generate(_req(deterministic=True, interactive=False))
    assert resp.withheld is False, "eval path must never abstain (golden safety)"
    assert resp.withhold_reason is None
    assert isinstance(resp.draft, str) and resp.draft.strip()
    assert resp.quality_score == 0.05


# --- (v) threshold reads the config knob -----------------------------------

def test_threshold_reads_config_knob(monkeypatch):
    _stub_pipeline(monkeypatch, quality=0.60)
    # Default floor is 0.5, so 0.60 would NOT abstain. Raise the configured
    # min_quality to 0.7 and the same draft must now be withheld.
    monkeypatch.setattr(svc, "_abstain_config", lambda: {"enabled": True, "min_quality": 0.7})
    resp = _generate(_req(interactive=False, deterministic=False))
    assert resp.withheld is True
    assert "0.70" in (resp.withhold_reason or "")


def test_disabled_config_never_abstains(monkeypatch):
    _stub_pipeline(monkeypatch, quality=0.01)
    monkeypatch.setattr(svc, "_abstain_config", lambda: {"enabled": False, "min_quality": 0.5})
    resp = _generate(_req(interactive=False, deterministic=False))
    assert resp.withheld is False


def test_default_threshold_is_the_auto_push_floor():
    # The abstain default reuses the shared quality floor constant — one policy.
    assert svc._abstain_config()["min_quality"] == svc.DEFAULT_QUALITY_FLOOR
    assert svc.DEFAULT_QUALITY_FLOOR == 0.5


def test_missing_quality_score_does_not_abstain(monkeypatch):
    # A None score (scoring threw) is NOT withheld here — the autonomous
    # auto-push gate already treats a missing score as below-floor and holds it
    # from acting, so we don't withhold a draft merely because scoring failed.
    _stub_pipeline(monkeypatch, quality=None)
    resp = _generate(_req(interactive=False, deterministic=False))
    assert resp.withheld is False


# --- direct unit test of the gate predicate --------------------------------

def test_should_abstain_gate_matrix(monkeypatch):
    monkeypatch.setattr(svc, "_abstain_config", lambda: {"enabled": True, "min_quality": 0.5})
    # autonomous + low -> abstain
    w, r = svc._should_abstain(_req(interactive=False, deterministic=False), 0.3)
    assert w is True and r
    # autonomous + high -> no
    assert svc._should_abstain(_req(interactive=False, deterministic=False), 0.9)[0] is False
    # interactive low -> no
    assert svc._should_abstain(_req(interactive=True, deterministic=False), 0.1)[0] is False
    # deterministic low -> no
    assert svc._should_abstain(_req(interactive=False, deterministic=True), 0.1)[0] is False
    # missing score -> no
    assert svc._should_abstain(_req(interactive=False, deterministic=False), None)[0] is False


# --- (vi) never-send: no send/act seam reachable on any path ---------------

def test_never_send_no_outbound_seam_on_abstain(monkeypatch):
    """Abstain produces FEWER outbound-eligible drafts, never more. Prove the
    drafting path reaches no send/push/Gmail-write seam regardless of outcome."""
    import app.generation.service as _svc

    forbidden = ("send_email", "push_pending_row", "create_draft", "_maybe_auto_send", "_maybe_auto_push")
    for name in forbidden:
        assert not hasattr(_svc, name), f"generation.service must not expose send seam {name!r}"

    # Both arms (withheld and not) return a DraftResponse only — no side-effect
    # send happens inside generate_draft.
    _stub_pipeline(monkeypatch, quality=0.2)
    r1 = _generate(_req(interactive=False, deterministic=False))
    assert isinstance(r1, _svc.DraftResponse) and r1.withheld is True
    _stub_pipeline(monkeypatch, quality=0.9)
    r2 = _generate(_req(interactive=False, deterministic=False))
    assert isinstance(r2, _svc.DraftResponse) and r2.withheld is False
