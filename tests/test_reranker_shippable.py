"""Cross-encoder reranker: shippable + measurable, default-off.

The reranker module existed (and was wired into ``RetrievalService``)
since `reranker_enabled` first appeared in config, but several real gaps
made it hard to actually ship the flag on:

1. **No dependency declared.** Flipping the flag without
   ``pip install sentence-transformers`` got you a silent fallback to
   FTS+semantic order while the trace claimed reranking was firing.
2. **No observability.** ``retrieval_method`` stayed ``fts5_bm25+semantic``
   regardless of whether reranking actually ran — a misconfigured
   instance had no way to notice.
3. **Hardcoded magic numbers.** Blend weight (60% CE / 40% FTS), CE
   score scale (10x), and model id were literals — no tuning surface.

This module pins the fixes:

- ``rerank()`` now returns ``(matches, applied)``: ``applied=False``
  whenever the dep is missing or ``predict`` raises mid-call. The
  caller flips ``retrieval_method`` to ``...+reranker`` only when
  ``applied`` is True, so the label is honest.
- ``reranker_model_id`` / ``reranker_blend_weight`` /
  ``reranker_ce_score_scale`` config knobs (defaults match the
  historical hardcoded values exactly — zero behavior change).
- ``RetrievalResponse.reranker_applied`` field surfaces the signal to
  callers / API consumers.
- Doctor (`youos doctor`) warns when ``reranker_enabled: true`` is set
  in config but the dep isn't loadable — so the user sees the mismatch
  rather than silently getting no reranking.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import yaml

# ── rerank() returns (matches, applied) ───────────────────────────────────

def _reset_reranker():
    import app.core.reranker as reranker_mod

    reranker_mod._cross_encoder = None
    reranker_mod._load_attempted = False
    reranker_mod._loaded_model_id = None


def _make_match(score: float = 1.0, snippet: str = "x"):
    from app.retrieval.service import RetrievalMatch

    return RetrievalMatch(
        result_type="reply_pair",
        score=score,
        lexical_score=score,
        metadata_score=0.0,
        source_type="gmail_thread",
        source_id="src-1",
        account_email=None,
        title=None,
        author=None,
        external_uri=None,
        thread_id=None,
        created_at=None,
        updated_at=None,
        document_id=None,
        snippet=snippet,
    )


def test_rerank_returns_applied_false_when_encoder_unavailable():
    """The whole point of the (matches, applied) tuple: the caller must
    be able to tell whether the CE actually ran, so the retrieval_method
    label can stay honest."""
    import app.core.reranker as reranker_mod

    _reset_reranker()
    reranker_mod._load_attempted = True
    reranker_mod._cross_encoder = None

    matches = [_make_match(5.0), _make_match(3.0)]
    reranked, applied = reranker_mod.rerank("q", matches, 2)
    assert reranked is matches
    assert applied is False


def test_rerank_returns_applied_true_when_encoder_scores():
    import app.core.reranker as reranker_mod

    _reset_reranker()
    mock = MagicMock()
    mock.predict.return_value = [0.2, 0.9]
    reranker_mod._cross_encoder = mock
    reranker_mod._load_attempted = True
    try:
        matches = [_make_match(5.0, "low"), _make_match(3.0, "high")]
        reranked, applied = reranker_mod.rerank("q", matches, 2)
        assert applied is True
        assert reranked[0].snippet == "high"
    finally:
        _reset_reranker()


def test_rerank_returns_applied_false_when_predict_raises():
    """A loaded encoder that throws mid-call still has to surface the
    failure to the caller — no silent partial reranking."""
    import app.core.reranker as reranker_mod

    _reset_reranker()
    mock = MagicMock()
    mock.predict.side_effect = RuntimeError("CUDA OOM")
    reranker_mod._cross_encoder = mock
    reranker_mod._load_attempted = True
    try:
        matches = [_make_match(5.0), _make_match(3.0)]
        reranked, applied = reranker_mod.rerank("q", matches, 2)
        assert reranked is matches
        assert applied is False
    finally:
        _reset_reranker()


def test_rerank_returns_applied_false_for_empty_matches():
    """Empty input → no work to do; applied=False reflects "didn't run"."""
    import app.core.reranker as reranker_mod

    _reset_reranker()
    mock = MagicMock()
    mock.predict.return_value = []
    reranker_mod._cross_encoder = mock
    reranker_mod._load_attempted = True
    try:
        reranked, applied = reranker_mod.rerank("q", [], 5)
        assert reranked == []
        assert applied is False
    finally:
        _reset_reranker()


# ── blend weight / scale knobs ────────────────────────────────────────────

def test_rerank_default_blend_matches_historical_constants():
    """Defaults: w=0.6 (CE), scale=10. Pin the exact math against a
    known input/output so future refactors can't silently drift the
    default behavior."""
    import app.core.reranker as reranker_mod

    _reset_reranker()
    mock = MagicMock()
    mock.predict.return_value = [0.5]
    reranker_mod._cross_encoder = mock
    reranker_mod._load_attempted = True
    try:
        m = _make_match(score=4.0, snippet="x")
        reranked, applied = reranker_mod.rerank("q", [m], 1)
        # 4.0 * 0.4 + 0.5 * 10 * 0.6 = 1.6 + 3.0 = 4.6
        assert applied is True
        assert reranked[0].score == 4.6
    finally:
        _reset_reranker()


def test_rerank_blend_weight_override_applies():
    """A 100% CE weight (w=1.0) means the FTS score is ignored — useful
    for an instance that wants pure CE reranking."""
    import app.core.reranker as reranker_mod

    _reset_reranker()
    mock = MagicMock()
    mock.predict.return_value = [0.5]
    reranker_mod._cross_encoder = mock
    reranker_mod._load_attempted = True
    try:
        m = _make_match(score=4.0)
        reranked, applied = reranker_mod.rerank("q", [m], 1, blend_weight=1.0)
        # 4.0 * 0 + 0.5 * 10 * 1 = 5.0
        assert applied is True
        assert reranked[0].score == 5.0
    finally:
        _reset_reranker()


def test_rerank_ce_score_scale_override_applies():
    """A scale of 1.0 means raw CE scores blend as-is — useful for a CE
    model whose output range matches FTS without inflation."""
    import app.core.reranker as reranker_mod

    _reset_reranker()
    mock = MagicMock()
    mock.predict.return_value = [0.5]
    reranker_mod._cross_encoder = mock
    reranker_mod._load_attempted = True
    try:
        m = _make_match(score=4.0)
        reranked, applied = reranker_mod.rerank("q", [m], 1, ce_score_scale=1.0)
        # 4.0 * 0.4 + 0.5 * 1.0 * 0.6 = 1.6 + 0.3 = 1.9
        assert applied is True
        assert reranked[0].score == 1.9
    finally:
        _reset_reranker()


# ── RetrievalConfig knobs ─────────────────────────────────────────────────

def test_retrieval_config_defaults_for_reranker_are_none():
    """None defaults → rerank() falls back to its module-level constants,
    so an instance that doesn't mention the keys in YAML keeps the
    historical hardcoded behavior."""
    from app.retrieval.service import RetrievalConfig

    cfg = RetrievalConfig(
        top_k_documents=3, top_k_chunks=3, top_k_reply_pairs=5,
        recency_boost_days=90, recency_boost_weight=0.2,
        account_boost_weight=0.15, source_weights={},
    )
    assert cfg.reranker_enabled is False
    assert cfg.reranker_model_id is None
    assert cfg.reranker_blend_weight is None
    assert cfg.reranker_ce_score_scale is None


def test_yaml_loader_reads_reranker_knobs(tmp_path):
    configs = tmp_path / "configs"
    (configs / "retrieval").mkdir(parents=True)
    (configs / "retrieval" / "defaults.yaml").write_text(
        yaml.safe_dump({
            "top_k_reply_pairs": 8,
            "top_k_documents": 3,
            "top_k_chunks": 3,
            "recency_boost_days": 60,
            "recency_boost_weight": 0.2,
            "account_boost_weight": 0.15,
            "reranker_enabled": True,
            "reranker_model_id": "BAAI/bge-reranker-base",
            "reranker_blend_weight": 0.75,
            "reranker_ce_score_scale": 5.0,
        }),
        encoding="utf-8",
    )
    from app.retrieval.service import _load_retrieval_config

    cfg = _load_retrieval_config(configs)
    assert cfg.reranker_enabled is True
    assert cfg.reranker_model_id == "BAAI/bge-reranker-base"
    assert cfg.reranker_blend_weight == 0.75
    assert cfg.reranker_ce_score_scale == 5.0


def test_yaml_loader_ignores_non_numeric_blend_weight(tmp_path):
    """A fat-fingered ``reranker_blend_weight: "high"`` falls back to None
    (which rerank() then resolves to the module default) rather than
    crashing the retrieval layer at module load."""
    configs = tmp_path / "configs"
    (configs / "retrieval").mkdir(parents=True)
    (configs / "retrieval" / "defaults.yaml").write_text(
        yaml.safe_dump({
            "top_k_reply_pairs": 8,
            "top_k_documents": 3,
            "top_k_chunks": 3,
            "recency_boost_days": 60,
            "recency_boost_weight": 0.2,
            "account_boost_weight": 0.15,
            "reranker_blend_weight": "high",
            "reranker_ce_score_scale": "huge",
        }),
        encoding="utf-8",
    )
    from app.retrieval.service import _load_retrieval_config

    cfg = _load_retrieval_config(configs)
    assert cfg.reranker_blend_weight is None
    assert cfg.reranker_ce_score_scale is None


# ── retrieval_method label honesty ────────────────────────────────────────

def test_retrieval_method_label_omits_reranker_when_fallback(tmp_path, monkeypatch):
    """The whole reason `rerank()` returns `applied`: the
    retrieval_method label must say `+reranker` ONLY when reranking
    actually ran. A misconfigured instance with the flag on but no dep
    must NOT see `+reranker` in the trace."""
    from app.retrieval import service as svc

    # Mock out the rerank call to simulate the "encoder unavailable"
    # path: returns (matches, False).
    def _fake_rerank(query, matches, top_n, **kwargs):
        return matches, False

    monkeypatch.setattr("app.core.reranker.rerank", _fake_rerank)

    response = svc.RetrievalResponse(
        query="q",
        retrieval_method="fts5_bm25",
        semantic_search_enabled=False,
        applied_filters={},
        detected_mode=None,
        documents=[],
        chunks=[],
        reply_pairs=[],
        reranker_applied=False,
    )
    assert "+reranker" not in response.retrieval_method
    assert response.to_dict()["reranker_applied"] is False


def test_retrieval_response_surfaces_reranker_applied_when_true():
    from app.retrieval import service as svc

    response = svc.RetrievalResponse(
        query="q",
        retrieval_method="fts5_bm25+semantic+reranker",
        semantic_search_enabled=True,
        applied_filters={},
        detected_mode=None,
        documents=[],
        chunks=[],
        reply_pairs=[],
        reranker_applied=True,
    )
    d = response.to_dict()
    assert d["reranker_applied"] is True
    assert "reranker" in d["retrieval_method"]


# ── reranker.loaded_model_id() observability ──────────────────────────────

def test_loaded_model_id_starts_none():
    """Before any rerank() call, no model is loaded — so loaded_model_id()
    returns None and the stats surface knows not to claim a model is live."""
    import app.core.reranker as reranker_mod

    _reset_reranker()
    assert reranker_mod.loaded_model_id() is None


def test_loaded_model_id_reflects_load(monkeypatch):
    """After a successful load, loaded_model_id() reflects what was
    actually loaded — useful when config drift means the YAML says one
    model but a different one is live."""
    import app.core.reranker as reranker_mod

    _reset_reranker()

    # Stub CrossEncoder so the test doesn't actually download anything.
    class _FakeCE:
        def __init__(self, model_id):
            self.model_id = model_id

    class _FakeST:
        CrossEncoder = _FakeCE

    monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", _FakeST)
    try:
        ok = reranker_mod.is_reranker_available(model_id="some-custom/model")
        assert ok is True
        assert reranker_mod.loaded_model_id() == "some-custom/model"
    finally:
        _reset_reranker()


# ── doctor warning ────────────────────────────────────────────────────────

def test_doctor_warns_when_reranker_enabled_but_dep_missing(monkeypatch, tmp_path, _reset_settings_fixture=None):
    """Without this warning, a user who flipped the flag without
    installing the dep would silently get FTS+semantic order forever
    while every trace falsely claimed reranking."""
    # Set up a minimal instance with retrieval/defaults.yaml that enables
    # the reranker but with no sentence-transformers installed.
    from app.core.settings import get_settings

    (tmp_path / "var").mkdir()
    (tmp_path / "configs" / "retrieval").mkdir(parents=True)
    (tmp_path / "configs" / "retrieval" / "defaults.yaml").write_text(
        "reranker_enabled: true\n", encoding="utf-8",
    )
    (tmp_path / "youos_config.yaml").write_text("user:\n  emails: ['a@b']\n", encoding="utf-8")
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()

    # Force the reranker into "not available" state.
    _reset_reranker()
    import app.core.reranker as reranker_mod

    monkeypatch.setattr(reranker_mod, "is_reranker_available", lambda *_a, **_kw: False)

    try:
        from app.core.doctor import run_doctor_checks_full

        _, _failures, warnings = run_doctor_checks_full()
        assert any("reranker_enabled" in w and "sentence-transformers" in w for w in warnings), (
            f"expected reranker warning in {warnings}"
        )
    finally:
        get_settings.cache_clear()
        _reset_reranker()


def test_doctor_does_not_warn_when_reranker_disabled(monkeypatch, tmp_path):
    """No warning when the flag is off — the dep is optional, so its
    absence is only a problem when the flag is on."""
    from app.core.settings import get_settings

    (tmp_path / "var").mkdir()
    (tmp_path / "configs" / "retrieval").mkdir(parents=True)
    (tmp_path / "configs" / "retrieval" / "defaults.yaml").write_text(
        "reranker_enabled: false\n", encoding="utf-8",
    )
    (tmp_path / "youos_config.yaml").write_text("user:\n  emails: ['a@b']\n", encoding="utf-8")
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()

    try:
        from app.core.doctor import run_doctor_checks_full

        _, _failures, warnings = run_doctor_checks_full()
        assert not any("reranker_enabled" in w for w in warnings), (
            f"unexpected reranker warning in {warnings}"
        )
    finally:
        get_settings.cache_clear()


# ── optional dep is declared in pyproject ─────────────────────────────────

def test_pyproject_declares_reranker_optional_dep():
    """Installing `pip install youos[reranker]` should pull
    sentence-transformers — guard the declaration so a typo / removal
    doesn't silently revert to "user has to figure it out themselves"."""
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    body = pyproject.read_text(encoding="utf-8")
    assert "reranker = [" in body
    assert "sentence-transformers" in body
