"""End-to-end gate test (b286): a fabrication in the generated draft must flow
through the REAL verifier → collapse quality → abstain (withheld) on the
autonomous path, and the fabrication category must be recorded for telemetry.

Hermetic: stubs the retrieval/persona/model dispatch of generate_draft (mirrors
tests/test_abstain_gating_b188.py) but keeps verify_draft LIVE. The per-draft
quality is pinned HIGH (0.8) so that the ONLY thing that can withhold the draft
is the verify collapse — proving the chain, not the threshold.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import app.generation.service as svc


def _stub(monkeypatch, *, draft_text: str):
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
    monkeypatch.setattr(svc, "_connect", lambda _p: sqlite3.connect(":memory:"))
    monkeypatch.setattr(svc, "resolve_sqlite_path", lambda _u: Path("/tmp/x.db"))
    monkeypatch.setattr(svc, "_resolve_decoding", lambda intent, conf: (None, None))
    monkeypatch.setattr(svc, "_multi_candidate_config", lambda: {"enabled": False, "temperatures": [0.3]})
    monkeypatch.setattr(svc, "_signoff_name", lambda: "Baher Al Hakim")
    monkeypatch.setattr(svc, "_call_local_model", lambda prompt, **kw: draft_text)
    # High quality: only a verify collapse can withhold the draft.
    monkeypatch.setattr(svc, "draft_quality_score", lambda *a, **kw: 0.8)
    # Capture the verify_flags that would be persisted to draft_events.
    captured: dict = {}
    monkeypatch.setattr(svc, "_log_draft_event", lambda *a, **kw: captured.update(kw) or True)
    return captured


def _gen(**kw):
    req = svc.DraftRequest(inbound_message="Can we meet next week?", **kw)
    return svc.generate_draft(req, database_url="sqlite:///x", configs_dir=Path("/tmp"))


def test_fabrication_collapses_quality_and_withholds(monkeypatch):
    captured = _stub(
        monkeypatch,
        draft_text="Hi — sounds good, next week works. Wishing you well; "
                   "enjoy the time with the new baby!",
    )
    resp = _gen(interactive=False, deterministic=False)
    # Verify ran live, saw an ungrounded family detail, collapsed the 0.8 → ≤0.1.
    assert resp.quality_score is not None and resp.quality_score <= 0.1
    # Autonomous + collapsed below the floor → withheld for review.
    assert resp.withheld is True
    # Telemetry: the fabrication category was recorded on the draft event.
    assert "fabrication" in captured.get("verify_flags", [])


def test_clean_draft_passes_and_logs_no_flags(monkeypatch):
    captured = _stub(
        monkeypatch,
        draft_text="Hi — next week works for me. Tuesday or Wednesday afternoon?",
    )
    resp = _gen(interactive=False, deterministic=False)
    assert resp.withheld is False
    assert resp.quality_score == 0.8
    assert captured.get("verify_flags", []) == []


def test_interactive_fabrication_not_withheld_but_flag_still_logged(monkeypatch):
    # A human who asked for the draft always gets it; the flag is still recorded.
    captured = _stub(
        monkeypatch,
        draft_text="Hi — congrats, enjoy the new baby! Let's meet next week.",
    )
    resp = _gen(interactive=True, deterministic=False)
    assert resp.withheld is False
    assert "fabrication" in captured.get("verify_flags", [])
