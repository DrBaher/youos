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
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {})
    cfg = _multi_candidate_config()
    assert cfg["enabled"] is False
    assert cfg["temperatures"] == [0.3, 0.7, 1.0]


def test_multi_candidate_reads_config(monkeypatch):
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **k: {"generation": {"multi_candidate": {"enabled": True, "temperatures": [0.5, 0.9]}}},
    )
    cfg = _multi_candidate_config()
    assert cfg["enabled"] is True
    assert cfg["temperatures"] == [0.5, 0.9]


def test_multi_candidate_bad_config_falls_back(monkeypatch):
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {"generation": {"multi_candidate": "nope"}})
    assert _multi_candidate_config() == {"enabled": False, "temperatures": [0.3, 0.7, 1.0]}


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

    def fake_once(prompt, *, max_tokens, temperature, top_p, request, sender_type_hint):
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
    resp = svc.generate_draft(svc.DraftRequest(inbound_message="hi"), database_url="sqlite:///x", configs_dir=Path("/tmp"))

    assert sorted(seen) == [0.3, 0.7, 1.0]            # one call per temperature
    assert len(resp.candidates) == 3                  # alternatives surfaced
    assert resp.candidates[0]["temperature"] == 0.7   # on-target ranked first
    assert resp.draft == " ".join(["w"] * 30)         # best chosen


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
