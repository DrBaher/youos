"""Length calibration / control (b187).

Today length is MEASURED but not CONTROLLED: the prompt only suggests "~N
words", max_tokens is a loose avg×5, and multi-candidate ranking ignores
length-fit. b187 adds real control:

  (i)   a persona length BAND (p25–p75 if present, else [0.6·avg, 1.4·avg]),
        the single source of truth for the flag / ranking / token budget;
  (ii)  a band-derived max_tokens (upper-edge tokens + headroom), not avg×5;
  (iii) length-fit folded into multi-candidate ranking — an in-band candidate
        beats an out-of-band one of equal quality;
  (iv)  a concise-retry on a LONG draft that is INERT on the deterministic/eval
        path (b166 reproducibility) and fires exactly once on a long LIVE draft;
  (v)   the never-send invariant is untouched (drafting never sends).

These pin the band derivation, the budget→band tie, the ranking preference, the
retry gating, and that none of it can send mail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.generation import service as svc

# --- (i) band derivation from persona stats --------------------------------


def test_band_from_percentiles_when_present():
    # p25–p75 is the tighter, data-grounded band — preferred over the average.
    assert svc._length_band(40, p25=34, p75=37) == (34, 37)


def test_band_falls_back_to_multiplicative_spread():
    # No percentiles -> [0.6·avg, 1.4·avg] around the average.
    assert svc._length_band(40) == (24, 56)
    assert svc._length_band(30) == (18, 42)


def test_band_ignores_degenerate_percentiles():
    # p25 > p75 (or non-positive) is not a usable window -> fall back to avg.
    assert svc._length_band(40, p25=37, p75=34) == (24, 56)
    assert svc._length_band(40, p25=0, p75=0) == (24, 56)


def test_band_none_without_target():
    assert svc._length_band(None) is None
    assert svc._length_band(0) is None


def test_length_flag_uses_the_band():
    # The flag and the band agree: inside -> ok, below low -> short, above high.
    assert svc._length_flag("w " * 40, 40) == "ok"          # 40 in [24,56]
    assert svc._length_flag("w " * 10, 40) == "short"        # 10 < 24
    assert svc._length_flag("w " * 70, 40) == "long"         # 70 > 56
    # Percentile band is tighter: 40 words is "long" against a [34,37] window.
    assert svc._length_flag("w " * 40, 40, p25=34, p75=37) == "long"


# --- (ii) max_tokens is tied to the band, not avg×5 ------------------------


def test_max_tokens_tied_to_band_not_avg_times_five():
    # avg=40 -> band high=56 -> ~56·1.5·2.0 = 168 tokens, NOT the old 40·5=200.
    mt = svc._compute_max_tokens(40)
    assert mt != 200, "must not be the old avg×5 budget"
    expected = int(round(56 * svc._TOKENS_PER_WORD * svc._MAX_TOKENS_HEADROOM))
    assert mt == max(100, min(500, expected))


def test_max_tokens_follows_percentile_band():
    # With a tighter percentile band the budget tracks the band's high edge.
    mt = svc._compute_max_tokens(40, p25=34, p75=37)
    expected = int(round(37 * svc._TOKENS_PER_WORD * svc._MAX_TOKENS_HEADROOM))
    assert mt == max(100, min(500, expected))
    # Tighter band -> strictly smaller budget than the wide multiplicative one.
    assert mt < svc._compute_max_tokens(40)


def test_max_tokens_clamped_and_default():
    assert svc._compute_max_tokens(None) == 300
    assert 100 <= svc._compute_max_tokens(5) <= 500
    assert 100 <= svc._compute_max_tokens(1000) <= 500


# --- (iii) length-fit changes multi-candidate ranking ----------------------


def test_in_band_candidate_wins_at_equal_quality(monkeypatch):
    # Two candidates, IDENTICAL voice/quality (no exemplars -> voice=None, so
    # quality is purely structural): one in-band, one far over the band. The
    # in-band one must rank first because of the length-fit bonus/penalty.
    target = 30  # band [18,42]
    in_band = " ".join(["word"] * 30)         # 30 in band -> ok
    too_long = " ".join(["word"] * 90)        # 90 >> 42 -> long

    ranked = svc._rank_candidates_by_quality(
        [(too_long, "lora", 0.7), (in_band, "lora", 0.3)],
        reply_pairs=None, target_words=target, greeting="", closing="",
    )
    assert ranked[0]["draft"] == in_band, "in-band candidate must win"
    assert ranked[0]["quality_score"] >= ranked[1]["quality_score"]


def test_score_candidate_penalizes_out_of_band():
    # An in-band draft scores strictly higher than an out-of-band one of the
    # same greeting/closing status — the length-fit signal the ranker uses.
    in_band = " ".join(["word"] * 30)
    too_long = " ".join(["word"] * 90)
    s_in = svc._score_candidate(in_band, target_words=30, greeting="", closing="")
    s_out = svc._score_candidate(too_long, target_words=30, greeting="", closing="")
    assert s_in > s_out


# --- (iv) concise-retry: inert on eval, fires once on a long LIVE draft -----


def _stub_pipeline(monkeypatch, *, avg_words=30, multi_candidate=False):
    """Stub everything generate_draft touches up to the model dispatch
    (mirrors tests/test_eval_determinism_b166.py)."""
    def _stub_retrieve(*a, **kw):
        return svc.RetrievalResponse(
            query="", retrieval_method="x", semantic_search_enabled=False,
            applied_filters={}, detected_mode=None, documents=[], chunks=[], reply_pairs=[],
        )

    monkeypatch.setattr(svc, "retrieve_context", _stub_retrieve)
    monkeypatch.setattr(svc, "_load_prompts", lambda _d: {"system_prompt": "S"})
    monkeypatch.setattr(svc, "_load_persona", lambda _d: {"style": {"avg_reply_words": avg_words}, "modes": {}})
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
        lambda: {"enabled": multi_candidate, "n": 3, "temperatures": [0.3, 0.7, 1.0]},
    )


def _long_then_short(monkeypatch):
    """Patch the model seam: first call returns a LONG draft (above band),
    every later call returns a short in-band draft. Returns the call log."""
    long_draft = " ".join(["word"] * 90)      # > band high (42) -> long
    short_draft = " ".join(["word"] * 25)     # in band [18,42] -> ok
    calls: list[dict] = []

    def fake_local(prompt, *, max_tokens=300, use_adapter=True, adapter_path=None,
                   temperature=None, top_p=None, seed=None):
        calls.append({"max_tokens": max_tokens, "seed": seed, "temperature": temperature})
        return long_draft if len(calls) == 1 else short_draft

    monkeypatch.setattr(svc, "_call_local_model", fake_local)
    return calls


def _req(**kw):
    return svc.DraftRequest(inbound_message="Can we meet next week?", **kw)


def _generate(req):
    return svc.generate_draft(req, database_url="sqlite:///x", configs_dir=Path("/tmp"))


def test_concise_retry_inert_on_deterministic_path(monkeypatch):
    # Eval/golden path: even though the (greedy) draft is long, NO retry fires —
    # exactly one generation call, so reproducibility (b166) is preserved.
    _stub_pipeline(monkeypatch)
    calls = _long_then_short(monkeypatch)
    resp = _generate(_req(deterministic=True, seed=svc.EVAL_SEED))
    assert len(calls) == 1, "deterministic path must not retry"
    assert resp.length_flag == "long", "the long draft is returned unchanged on eval"


def test_concise_retry_fires_once_on_long_live_draft(monkeypatch):
    # LIVE path: a long draft triggers exactly ONE concise-retry, and the
    # better-fitting (in-band) retry result is kept.
    _stub_pipeline(monkeypatch)
    calls = _long_then_short(monkeypatch)
    resp = _generate(_req())  # deterministic defaults False -> live path
    assert len(calls) == 2, "exactly one concise-retry on a long live draft"
    assert resp.length_flag == "ok", "the tighter in-band retry is kept"
    # The retry must use a TIGHTER token budget than the first generation.
    assert calls[1]["max_tokens"] < calls[0]["max_tokens"]


def test_no_retry_when_first_draft_in_band(monkeypatch):
    # A live draft already in band must NOT trigger a retry.
    _stub_pipeline(monkeypatch)
    in_band = " ".join(["word"] * 25)
    calls: list[dict] = []

    def fake_local(prompt, *, max_tokens=300, **kw):
        calls.append({"max_tokens": max_tokens})
        return in_band

    monkeypatch.setattr(svc, "_call_local_model", fake_local)
    resp = _generate(_req())
    assert len(calls) == 1, "in-band draft needs no retry"
    assert resp.length_flag == "ok"


# --- (v) never-send invariant ----------------------------------------------


def test_drafting_never_sends(monkeypatch):
    # Length control runs inside generate_draft, which has no send path at all.
    # Guard belt-and-suspenders: if any send seam were reachable it would raise.
    import app.ingestion.gmail_write as gw

    monkeypatch.setattr(
        gw, "send_draft",
        lambda **kw: (_ for _ in ()).throw(AssertionError("drafting must never send")),
        raising=False,
    )
    _stub_pipeline(monkeypatch)
    _long_then_short(monkeypatch)
    # Both the live (retry) and eval paths must complete without sending.
    _generate(_req())
    _generate(_req(deterministic=True, seed=svc.EVAL_SEED))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
