"""Regression test for b166: autoresearch eval generation must be deterministic.

Eval/golden generation must force greedy (temperature=0) + a fixed seed on BOTH
the warm-model-server path and the cold mlx_lm subprocess path, while normal
user-facing drafting stays unchanged (config/model defaults, no seed).

Hermetic: no real mlx_lm, no model, no network. We stub the full retrieval /
persona / persistence path of generate_draft (mirroring tests/test_multi_candidate.py)
and patch the low-level model seam to capture the decode args it is handed.
"""
from __future__ import annotations

from pathlib import Path

import app.core.model_server as ms
import app.generation.service as svc

# Capture the real implementation at import time so the cold-subprocess tests,
# which call it directly, are immune to tests that monkeypatch the module attr.
_REAL_CALL_LOCAL_MODEL = svc._call_local_model


# --- shared stubbing -------------------------------------------------------

def _stub_pipeline(monkeypatch, *, decoding=(None, None), multi_candidate=False):
    """Stub everything generate_draft touches up to the model dispatch."""
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
    monkeypatch.setattr(svc, "_resolve_decoding", lambda intent, conf: decoding)
    monkeypatch.setattr(
        svc, "_multi_candidate_config",
        lambda: {"enabled": multi_candidate, "temperatures": [0.3, 0.7, 1.0]},
    )


def _capture_local(monkeypatch):
    """Patch the local-model seam; return a list recording the decode kwargs."""
    calls: list[dict] = []

    def fake_local(prompt, *, max_tokens=300, use_adapter=True, adapter_path=None,
                   temperature=None, top_p=None, seed=None):
        calls.append({"temperature": temperature, "top_p": top_p, "seed": seed,
                      "max_tokens": max_tokens})
        return "This is a drafted reply, long enough to pass the empty check."

    monkeypatch.setattr(svc, "_call_local_model", fake_local)
    return calls


def _req(**kw):
    return svc.DraftRequest(inbound_message="Can we meet next week?", **kw)


def _generate(req):
    return svc.generate_draft(req, database_url="sqlite:///x", configs_dir=Path("/tmp"))


# --- eval path forces greedy + seed ----------------------------------------

def test_eval_request_is_greedy_and_seeded(monkeypatch):
    _stub_pipeline(monkeypatch)
    calls = _capture_local(monkeypatch)
    _generate(_req(deterministic=True, seed=svc.EVAL_SEED))
    assert calls, "local model should have been called"
    c = calls[0]
    assert c["temperature"] == 0.0, "eval generation must be greedy (temperature=0)"
    assert c["seed"] == svc.EVAL_SEED, "eval generation must pass the fixed seed"
    assert c["top_p"] is None, "top_p dropped under argmax"


def test_eval_default_seed_used_when_unspecified(monkeypatch):
    _stub_pipeline(monkeypatch)
    calls = _capture_local(monkeypatch)
    # deterministic=True but no explicit seed -> falls back to EVAL_SEED.
    _generate(_req(deterministic=True))
    assert calls[0]["seed"] == svc.EVAL_SEED


def test_eval_overrides_config_sampling(monkeypatch):
    # Config asks for hot sampling; eval determinism must override it.
    _stub_pipeline(monkeypatch, decoding=(0.9, 0.8))
    calls = _capture_local(monkeypatch)
    _generate(_req(deterministic=True))
    c = calls[0]
    assert c["temperature"] == 0.0
    assert c["top_p"] is None
    assert c["seed"] == svc.EVAL_SEED


def test_eval_bypasses_multi_candidate_spread(monkeypatch):
    # Multi-candidate is enabled, but deterministic eval must NOT fan out a
    # temperature spread (that would reintroduce variance): exactly one call.
    _stub_pipeline(monkeypatch, multi_candidate=True)
    calls = _capture_local(monkeypatch)
    _generate(_req(deterministic=True))
    assert len(calls) == 1
    assert calls[0]["temperature"] == 0.0 and calls[0]["seed"] == svc.EVAL_SEED


# --- normal drafting path is unchanged -------------------------------------

def test_normal_drafting_not_forced_deterministic(monkeypatch):
    # No config decoding -> normal drafting passes neither temp nor seed.
    _stub_pipeline(monkeypatch)
    calls = _capture_local(monkeypatch)
    _generate(_req())
    c = calls[0]
    assert c["temperature"] is None, "normal drafting must not be forced to greedy"
    assert c["seed"] is None, "normal drafting must not be seeded"


def test_normal_drafting_still_honors_config_decoding(monkeypatch):
    _stub_pipeline(monkeypatch, decoding=(0.7, 0.95))
    calls = _capture_local(monkeypatch)
    _generate(_req())
    c = calls[0]
    assert c["temperature"] == 0.7 and c["top_p"] == 0.95
    assert c["seed"] is None, "normal drafting must not be seeded even with config decoding"


# --- two identical eval calls produce identical captured args --------------

def test_two_identical_eval_calls_match(monkeypatch):
    _stub_pipeline(monkeypatch)
    calls = _capture_local(monkeypatch)
    _generate(_req(deterministic=True, seed=svc.EVAL_SEED))
    _generate(_req(deterministic=True, seed=svc.EVAL_SEED))
    assert len(calls) == 2
    assert calls[0] == calls[1], "identical eval calls must produce identical decode args"


# --- the cold mlx_lm subprocess gets --seed + --temp 0 ---------------------

def test_cold_subprocess_passes_seed_and_temp_flags(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, *, timeout=120):
        import subprocess
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "drafted reply text here", "")

    monkeypatch.setattr(svc, "_run_subprocess", fake_run)
    monkeypatch.setattr(svc, "_strip_mlx_output", lambda s: s)
    monkeypatch.setattr(svc, "_get_base_model_id", lambda: "dummy-model")
    # Force the subprocess path (not warm server) by requesting the base model.
    _REAL_CALL_LOCAL_MODEL("hi", max_tokens=10, use_adapter=False, temperature=0.0, seed=svc.EVAL_SEED)
    cmd = captured["cmd"]
    assert "--seed" in cmd and str(svc.EVAL_SEED) in cmd, cmd
    assert "--temp" in cmd, cmd
    assert cmd[cmd.index("--temp") + 1] == "0.0", cmd


def test_cold_subprocess_no_seed_when_unset(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, *, timeout=120):
        import subprocess
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "drafted reply text here", "")

    monkeypatch.setattr(svc, "_run_subprocess", fake_run)
    monkeypatch.setattr(svc, "_strip_mlx_output", lambda s: s)
    monkeypatch.setattr(svc, "_get_base_model_id", lambda: "dummy-model")
    _REAL_CALL_LOCAL_MODEL("hi", max_tokens=10, use_adapter=False)
    assert "--seed" not in captured["cmd"]


# --- warm-server payload carries the seed ----------------------------------

def test_warm_server_payload_includes_seed():
    body = ms._payload("hi", max_tokens=10, temperature=0.0, top_p=None, stream=False, seed=svc.EVAL_SEED)
    assert body["seed"] == svc.EVAL_SEED
    assert body["temperature"] == 0.0


def test_warm_server_payload_omits_seed_when_unset():
    body = ms._payload("hi", max_tokens=10, temperature=None, top_p=None, stream=False)
    assert "seed" not in body
