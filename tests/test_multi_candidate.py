"""Multi-candidate generation + ranking (Draft PR D).

Optionally generate several drafts (a temperature spread) and return the
best by a deterministic scorer. Pins the scorer/ranker/config and the
end-to-end wiring (one model call per temperature, best chosen, alternatives
surfaced) — default-off, so a single call and empty `candidates` otherwise.
"""

from __future__ import annotations

from pathlib import Path

from app.generation import service as svc
from app.generation.service import (
    _is_usable_draft,
    _multi_candidate_config,
    _rank_candidates,
    _score_candidate,
)

# --- config ----------------------------------------------------------------


def test_multi_candidate_disabled_by_default(monkeypatch):
    # b186: canonical knob is ``n`` (default 1 = single-candidate / off).
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {})
    cfg = _multi_candidate_config()
    assert cfg["enabled"] is False
    assert cfg["n"] == 1


def test_multi_candidate_reads_explicit_temperatures(monkeypatch):
    # Legacy ``enabled: true`` + explicit ``temperatures`` is still honored; n
    # is inferred from the list length.
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **k: {"generation": {"multi_candidate": {"enabled": True, "temperatures": [0.5, 0.9]}}},
    )
    cfg = _multi_candidate_config()
    assert cfg["enabled"] is True
    assert cfg["n"] == 2
    assert cfg["temperatures"] == [0.5, 0.9]


def test_multi_candidate_n_knob_derives_diverse_spread(monkeypatch):
    # b186: ``n`` alone enables multi-candidate and derives a diverse spread.
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **k: {"generation": {"multi_candidate": {"n": 3}}},
    )
    cfg = _multi_candidate_config()
    assert cfg["enabled"] is True
    assert cfg["n"] == 3
    assert len(cfg["temperatures"]) == 3
    # Diverse: candidates must differ (a single repeated temp would not).
    assert len(set(cfg["temperatures"])) == 3


def test_multi_candidate_n1_is_off(monkeypatch):
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **k: {"generation": {"multi_candidate": {"n": 1}}},
    )
    cfg = _multi_candidate_config()
    assert cfg["enabled"] is False
    assert cfg["n"] == 1


def test_multi_candidate_bad_config_falls_back(monkeypatch):
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {"generation": {"multi_candidate": "nope"}})
    cfg = _multi_candidate_config()
    assert cfg["enabled"] is False
    assert cfg["n"] == 1


# --- usability + scorer ----------------------------------------------------


def test_is_usable_draft():
    assert _is_usable_draft(" ".join(["word"] * 10))
    assert not _is_usable_draft("hi")  # too few non-ws chars
    assert not _is_usable_draft("[no model available]")  # placeholder
    assert not _is_usable_draft("Best,")  # signature-only


def test_score_rewards_on_target_length():
    target = 30
    on = _score_candidate(" ".join(["w"] * 30), target_words=target, greeting="", closing="")
    short = _score_candidate(" ".join(["w"] * 8), target_words=target, greeting="", closing="")
    long = _score_candidate(" ".join(["w"] * 200), target_words=target, greeting="", closing="")
    assert on > short and on > long


def test_score_disqualifies_unusable():
    assert _score_candidate("[error]", target_words=30, greeting="", closing="") == float("-inf")


def test_score_credits_greeting_and_closing():
    base = " ".join(["w"] * 30)
    with_struct = f"Hi John,\n\n{base}\n\nBest,\nBaher"
    s_plain = _score_candidate(base, target_words=30, greeting="Hi John,", closing="Best,\nBaher")
    s_struct = _score_candidate(with_struct, target_words=30, greeting="Hi John,", closing="Best,\nBaher")
    assert s_struct > s_plain


# --- ranker ----------------------------------------------------------------


def test_rank_candidates_orders_best_first():
    raw = [
        (" ".join(["word"] * 12), "m", 0.3),    # short (but usable)
        (" ".join(["word"] * 30), "m", 0.7),    # on target
        (" ".join(["word"] * 200), "m", 1.0),   # long
    ]
    ranked = _rank_candidates(raw, target_words=30, greeting="", closing="")
    assert ranked[0]["temperature"] == 0.7
    assert [c["temperature"] for c in ranked] == [0.7, 0.3, 1.0]
    assert all("score" in c for c in ranked)
    # No exemplars → voice_match is None (backward compatible).
    assert all(c["voice_match"] is None for c in ranked)


def test_rank_candidates_prefers_voice_match_when_exemplars_given():
    """With the user's real replies as exemplars, the candidate that sounds
    more like them wins over an equally-long but stylistically-foreign one —
    voice, not length, decides."""
    exemplar = "Hi Alice, confirmed the pricing is unchanged. Let me know if you need anything else. Best, Baher"
    cand_a = "Hi Alice, confirmed the pricing is unchanged. Reach out if you need more. Best, Baher"
    n = len(cand_a.split())
    cand_b = " ".join(["lorem"] * n)  # same length, zero stylistic/lexical overlap
    # Feed B first so a stable sort can't accidentally favour A by position.
    raw = [(cand_b, "m", 0.7), (cand_a, "m", 0.3)]
    ranked = _rank_candidates(
        raw, target_words=n, greeting="", closing="", exemplar_replies=[exemplar],
    )
    assert ranked[0]["draft"] == cand_a
    assert ranked[0]["voice_match"] is not None
    assert (ranked[0]["voice_match"] or 0) > (ranked[1]["voice_match"] or 0)


# --- end-to-end wiring -----------------------------------------------------


def _stub(monkeypatch, *, load_config, persona, once_seen):
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: load_config)

    def _stub_retrieve(*a, **kw):
        return svc.RetrievalResponse(
            query="", retrieval_method="x", semantic_search_enabled=False,
            applied_filters={}, detected_mode=None, documents=[], chunks=[], reply_pairs=[],
        )

    monkeypatch.setattr(svc, "retrieve_context", _stub_retrieve)
    monkeypatch.setattr(svc, "_load_prompts", lambda _d: {"system_prompt": "S"})
    monkeypatch.setattr(svc, "_load_persona", lambda _d: persona)
    monkeypatch.setattr(svc, "lookup_sender_profile", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "lookup_facts", lambda **kw: [])
    monkeypatch.setattr(svc, "_lookup_prior_reply_to_sender", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "_local_model_available", lambda: True)
    monkeypatch.setattr(svc, "generate_subject", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "_log_draft_event", lambda *a, **kw: False)
    monkeypatch.setattr(svc, "_connect", lambda _p: __import__("sqlite3").connect(":memory:"))
    monkeypatch.setattr(svc, "resolve_sqlite_path", lambda _u: Path("/tmp/x.db"))

    drafts = {0.3: " ".join(["w"] * 10), 0.7: " ".join(["w"] * 30), 1.0: " ".join(["w"] * 200)}

    def fake_once(prompt, *, max_tokens, temperature, top_p, request, sender_type_hint, seed=None):
        once_seen.append(temperature)
        return drafts.get(temperature, " ".join(["w"] * 30)), "qwen2.5-1.5b-base"

    monkeypatch.setattr(svc, "_local_draft_once", fake_once)


def test_multi_candidate_generates_per_temperature_and_picks_best(monkeypatch):
    seen: list = []
    _stub(
        monkeypatch,
        load_config={"generation": {"multi_candidate": {"enabled": True, "temperatures": [0.3, 0.7, 1.0]}}},
        persona={"style": {"avg_reply_words": 30}, "modes": {}},
        once_seen=seen,
    )
    # b194: opt-in required — this mirrors the autonomous/eval path.
    resp = svc.generate_draft(
        svc.DraftRequest(inbound_message="hi", multi_candidate_ok=True),
        database_url="sqlite:///x", configs_dir=Path("/tmp"),
    )

    assert sorted(seen) == [0.3, 0.7, 1.0]            # one call per temperature
    assert len(resp.candidates) == 3                  # alternatives surfaced
    assert resp.candidates[0]["temperature"] == 0.7   # on-target ranked first
    assert resp.draft == " ".join(["w"] * 30)         # best chosen


def test_multi_candidate_gated_off_for_interactive_requests(monkeypatch):
    """b194: config n>1 must NOT fan out unless the request opts in
    (multi_candidate_ok=True). Interactive callers leave it False (the default),
    so they stay single-candidate / fast even when best-of-N is configured on."""
    seen: list = []
    _stub(
        monkeypatch,
        load_config={"generation": {"multi_candidate": {"n": 3}}},  # enabled
        persona={"style": {"avg_reply_words": 30}, "modes": {}},
        once_seen=seen,
    )
    # Default request: multi_candidate_ok=False, as every interactive endpoint.
    resp = svc.generate_draft(
        svc.DraftRequest(inbound_message="hi"),
        database_url="sqlite:///x", configs_dir=Path("/tmp"),
    )
    assert len(seen) == 1            # single model call despite config n=3
    assert resp.candidates == []     # no fan-out for interactive callers


def test_multi_candidate_fires_when_opted_in(monkeypatch):
    """b194: the autonomous triage sweep + compare-models set
    multi_candidate_ok=True and DO fan out under the same config."""
    seen: list = []
    _stub(
        monkeypatch,
        load_config={"generation": {"multi_candidate": {"n": 3}}},
        persona={"style": {"avg_reply_words": 30}, "modes": {}},
        once_seen=seen,
    )
    resp = svc.generate_draft(
        svc.DraftRequest(inbound_message="hi", multi_candidate_ok=True),
        database_url="sqlite:///x", configs_dir=Path("/tmp"),
    )
    assert len(seen) == 3            # one call per temperature in the spread
    assert len(resp.candidates) == 3


def test_exemplar_cache_bypassed_when_flag_false(monkeypatch):
    """use_exemplar_cache=False (the autoresearch eval path) must NOT consult or
    apply the exemplar cache — otherwise it pins exemplars across candidates and
    retrieval-param mutations become no-ops."""
    seen: list = []
    _stub(monkeypatch, load_config={}, persona={"style": {"avg_reply_words": 30}, "modes": {}}, once_seen=seen)

    calls = {"get": 0, "apply": 0}

    def _fake_get(*a, **k):
        calls["get"] += 1
        return [], False, None

    def _fake_apply(reply_pairs, cached_ids):
        calls["apply"] += 1
        return reply_pairs

    monkeypatch.setattr(svc, "_get_cached_exemplar_ids", _fake_get)
    monkeypatch.setattr(svc, "_apply_cached_order", _fake_apply)
    monkeypatch.setattr(svc, "_update_exemplar_cache", lambda *a, **k: None)

    # Default (True): cache consulted.
    svc.generate_draft(svc.DraftRequest(inbound_message="hi"), database_url="sqlite:///x", configs_dir=Path("/tmp"))
    assert calls["get"] == 1
    assert calls["apply"] == 1

    # Bypassed: neither read nor applied.
    calls["get"] = calls["apply"] = 0
    svc.generate_draft(
        svc.DraftRequest(inbound_message="hi", use_exemplar_cache=False),
        database_url="sqlite:///x", configs_dir=Path("/tmp"),
    )
    assert calls["get"] == 0
    assert calls["apply"] == 0


def test_single_candidate_path_when_disabled(monkeypatch):
    seen: list = []
    _stub(
        monkeypatch,
        load_config={},  # multi_candidate disabled
        persona={"style": {"avg_reply_words": 30}, "modes": {}},
        once_seen=seen,
    )
    resp = svc.generate_draft(svc.DraftRequest(inbound_message="hi"), database_url="sqlite:///x", configs_dir=Path("/tmp"))

    assert len(seen) == 1            # exactly one model call
    assert resp.candidates == []     # no alternatives in the default path
