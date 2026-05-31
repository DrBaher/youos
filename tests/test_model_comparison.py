"""Cross-model comparison + the backend_override pin it relies on.

Two layers:
  * generate_draft honours DraftRequest.backend_override (pins MLX/Ollama/Claude
    regardless of use_local_model / config) — the seam the comparison drives.
  * app/evaluation/model_compare.py aggregates voice-match across backends, flags
    silent fallbacks, samples real reply pairs, and renders a ranked scorecard.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.evaluation.model_compare import (
    BackendScore,
    ComparisonResult,
    _fell_back,
    compare_models,
    format_comparison,
    sample_reply_pairs,
)

# --- backend_override in generate_draft ------------------------------------


def _stub_generation(monkeypatch):
    """Stub generate_draft's I/O so only the backend-selection branch matters."""
    from app.generation import service as svc

    def _stub_retrieve(*a, **kw):
        return svc.RetrievalResponse(
            query="", retrieval_method="x", semantic_search_enabled=False,
            applied_filters={}, detected_mode=None, documents=[], chunks=[], reply_pairs=[],
        )

    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {})
    monkeypatch.setattr(svc, "retrieve_context", _stub_retrieve)
    monkeypatch.setattr(svc, "_load_prompts", lambda _d: {"system_prompt": "S"})
    monkeypatch.setattr(svc, "_load_persona", lambda _d: {"style": {"avg_reply_words": 30}, "modes": {}})
    monkeypatch.setattr(svc, "lookup_sender_profile", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "lookup_facts", lambda **kw: [])
    monkeypatch.setattr(svc, "_lookup_prior_reply_to_sender", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "_local_model_available", lambda: True)
    monkeypatch.setattr(svc, "generate_subject", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "_log_draft_event", lambda *a, **kw: False)
    monkeypatch.setattr(svc, "_connect", lambda _p: sqlite3.connect(":memory:"))
    monkeypatch.setattr(svc, "resolve_sqlite_path", lambda _u: Path("/tmp/x.db"))
    monkeypatch.setattr(svc, "get_model_fallback", lambda *a, **k: "none")

    usable = " ".join(["word"] * 20)
    monkeypatch.setattr(svc, "_local_draft_once", lambda *a, **kw: (usable, "qwen2.5-1.5b-base"))
    monkeypatch.setattr(svc, "_call_claude_cli", lambda *a, **kw: usable)
    monkeypatch.setattr("app.core.config.get_ollama_config",
                        lambda *a, **k: {"model": "mistral", "base_url": "http://localhost:11434"})
    monkeypatch.setattr(svc, "_generate_via_ollama", lambda *a, **kw: usable)
    return svc


def test_backend_override_pins_claude_over_local(monkeypatch):
    svc = _stub_generation(monkeypatch)
    # use_local_model True + MLX available, but override forces Claude.
    # b175: a cloud override now requires the cloud-escalation flag ON + the
    # per-request opt-in (both of which the real cross-model A/B sets via
    # _default_generate). Set them here so this seam still exercises the cloud arm.
    monkeypatch.setattr(svc, "cloud_escalation_enabled", lambda: True)
    resp = svc.generate_draft(
        svc.DraftRequest(
            inbound_message="hi", use_local_model=True, backend_override="claude",
            allow_cloud_escalation=True,
        ),
        database_url="sqlite:///x", configs_dir=Path("/tmp"),
    )
    assert resp.model_used == "claude"


def test_backend_override_claude_blocked_without_optin(monkeypatch):
    """b175: a cloud override with the flag OFF / no opt-in must NOT egress —
    it falls back to the local model instead of calling Claude."""
    svc = _stub_generation(monkeypatch)
    monkeypatch.setattr(svc, "cloud_escalation_enabled", lambda: False)
    resp = svc.generate_draft(
        # opt-in present but flag OFF -> still blocked
        svc.DraftRequest(
            inbound_message="hi", use_local_model=True, backend_override="claude",
            allow_cloud_escalation=True,
        ),
        database_url="sqlite:///x", configs_dir=Path("/tmp"),
    )
    assert resp.model_used != "claude"
    assert resp.cloud_used is False


def test_backend_override_pins_mlx(monkeypatch):
    svc = _stub_generation(monkeypatch)
    resp = svc.generate_draft(
        svc.DraftRequest(inbound_message="hi", use_local_model=False, backend_override="mlx"),
        database_url="sqlite:///x", configs_dir=Path("/tmp"),
    )
    assert resp.model_used == "qwen2.5-1.5b-base"


def test_backend_override_pins_ollama(monkeypatch):
    svc = _stub_generation(monkeypatch)
    resp = svc.generate_draft(
        svc.DraftRequest(inbound_message="hi", use_local_model=True, backend_override="ollama"),
        database_url="sqlite:///x", configs_dir=Path("/tmp"),
    )
    assert resp.model_used == "ollama:mistral"


def test_no_override_preserves_default_local(monkeypatch):
    svc = _stub_generation(monkeypatch)
    resp = svc.generate_draft(
        svc.DraftRequest(inbound_message="hi", use_local_model=True),
        database_url="sqlite:///x", configs_dir=Path("/tmp"),
    )
    assert resp.model_used == "qwen2.5-1.5b-base"


def test_forced_backend_drafts_are_not_logged(monkeypatch):
    """Benchmark drafts (backend_override set) must NOT pollute draft_events —
    otherwise compare-models corrupts the 'drafting with' reality signal."""
    from unittest.mock import MagicMock

    svc = _stub_generation(monkeypatch)
    logged = MagicMock(return_value=True)
    monkeypatch.setattr(svc, "_log_draft_event", logged)

    svc.generate_draft(
        svc.DraftRequest(inbound_message="hi", backend_override="claude"),
        database_url="sqlite:///x", configs_dir=Path("/tmp"),
    )
    logged.assert_not_called()

    # A normal draft (no override) still logs.
    svc.generate_draft(
        svc.DraftRequest(inbound_message="hi"),
        database_url="sqlite:///x", configs_dir=Path("/tmp"),
    )
    logged.assert_called_once()


# --- fell-back detection ---------------------------------------------------


def test_fell_back_detects_silent_fallback():
    # mlx that actually returned a Claude draft (empty-output retry) = fell back
    assert _fell_back("mlx", "claude") is True
    assert _fell_back("mlx", "qwen2.5-1.5b-base") is False
    assert _fell_back("ollama", "claude") is True
    assert _fell_back("ollama", "ollama:mistral") is False
    assert _fell_back("claude", "claude") is False
    assert _fell_back("claude", "error") is True


# --- compare_models aggregation -------------------------------------------


def _cases():
    return [
        {"case_key": "c1", "prompt_text": "Can we meet Thursday?",
         "reference_reply": "Sure, Thursday at 2pm works for me. Talk soon!"},
        {"case_key": "c2", "prompt_text": "Thoughts on the draft?",
         "reference_reply": "Looks good — just tighten the intro. Thanks!"},
    ]


def test_compare_ranks_by_voice_match_and_flags_fallback():
    cases = _cases()

    def fake_gen(prompt, *, backend, database_url, configs_dir):
        if backend == "mlx":
            # Near-verbatim to reference → high voice match.
            ref = {"Can we meet Thursday?": "Sure, Thursday at 2pm works for me. Talk soon!",
                   "Thoughts on the draft?": "Looks good — just tighten the intro. Thanks!"}[prompt]
            return {"draft": ref, "model_used": "qwen2.5-1.5b-base"}
        if backend == "claude":
            # Fluent but generic → lower voice match, and pretend it ran fine.
            return {"draft": "Thank you for your message. I would be delighted to assist with this matter.",
                    "model_used": "claude"}
        return {"draft": "", "model_used": "claude"}  # ollama empty → fell back to claude

    result = compare_models(
        cases, ["mlx", "claude", "ollama"],
        database_url="sqlite:///x", configs_dir=Path("/tmp"), generate_fn=fake_gen,
    )

    by = {s.backend: s for s in result.scores}
    assert by["mlx"].voice_match > by["claude"].voice_match     # local sounds more like you
    assert by["mlx"].n == 2 and by["mlx"].fallback_count == 0
    assert by["ollama"].fallback_count == 2                      # every ollama case fell back

    card = format_comparison(result)
    assert "Best voice-match: mlx" in card
    assert "fellbk" in card


def test_compare_counts_generation_errors():
    def boom(prompt, *, backend, database_url, configs_dir):
        raise RuntimeError("model crashed")

    result = compare_models(
        _cases(), ["mlx"], database_url="sqlite:///x", configs_dir=Path("/tmp"), generate_fn=boom,
    )
    assert result.scores[0].error_count == 2
    assert result.scores[0].n == 0  # nothing scored


def test_compare_semantic_flag_and_score():
    def fake_gen(prompt, *, backend, database_url, configs_dir):
        return {"draft": "Sounds great, see you then.", "model_used": "claude"}

    result = compare_models(
        _cases(), ["claude"], database_url="sqlite:///x", configs_dir=Path("/tmp"),
        generate_fn=fake_gen, embed_fn=lambda _t: (1.0, 0.0),
    )
    assert result.semantic is True
    assert result.scores[0].semantic_similarity is not None


def test_format_comparison_empty():
    result = ComparisonResult(backends=[], n_cases=0, scores=[], cases=[], semantic=False, run_at="now")
    assert "No cases scored" in format_comparison(result)


# --- sample_reply_pairs ----------------------------------------------------


def _seed_pairs_db(path: Path, n: int = 10) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY, inbound_text TEXT, reply_text TEXT)")
    for i in range(n):
        conn.execute(
            "INSERT INTO reply_pairs (inbound_text, reply_text) VALUES (?, ?)",
            (f"Inbound message number {i} with enough length to pass the filter here.",
             f"Reply number {i} that is also comfortably long enough to be a real reply."),
        )
    # One too-short pair that must be filtered out.
    conn.execute("INSERT INTO reply_pairs (inbound_text, reply_text) VALUES ('hi', 'ok')")
    conn.commit()
    conn.close()


def test_sample_reply_pairs_deterministic_and_filtered(tmp_path):
    db = tmp_path / "youos.db"
    _seed_pairs_db(db, n=10)
    url = f"sqlite:///{db}"

    cases = sample_reply_pairs(url, limit=5, min_chars=40, seed=7)
    assert len(cases) == 5
    assert all(c["reference_reply"] and c["prompt_text"] for c in cases)
    assert all(c["case_key"].startswith("pair-") for c in cases)
    # Deterministic for a fixed seed.
    again = sample_reply_pairs(url, limit=5, min_chars=40, seed=7)
    assert [c["case_key"] for c in cases] == [c["case_key"] for c in again]
    # The 'hi'/'ok' pair is below min_chars and never sampled.
    all_cases = sample_reply_pairs(url, limit=100, min_chars=40, seed=7)
    assert len(all_cases) == 10


def test_sample_reply_pairs_empty_db(tmp_path):
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY, inbound_text TEXT, reply_text TEXT)")
    conn.commit()
    conn.close()
    assert sample_reply_pairs(f"sqlite:///{db}", limit=5) == []


def test_backend_score_to_dict_roundtrips():
    s = BackendScore("mlx", 3, 0.7, None, 0.5, 0.6, 0.8, 42.0, 0, 0, 1.2)
    d = s.to_dict()
    assert d["backend"] == "mlx" and d["voice_match"] == 0.7
