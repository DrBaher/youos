"""Tests for retrieval improvements (Items 5-7, 12)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from app.retrieval.service import RetrievalConfig, RetrievalMatch


# --- Item 5: Sender-type retrieval boosting ---


def test_sender_type_boost_map_in_config():
    """Retrieval config loads sender_type_boost_map from defaults.yaml."""
    from app.retrieval.service import _load_retrieval_config

    configs_dir = Path(__file__).resolve().parents[1] / "configs"
    config = _load_retrieval_config(configs_dir)
    assert config.sender_type_boost_map
    assert config.sender_type_boost_map.get("external_client") == 1.3
    assert config.sender_type_boost_map.get("internal") == 1.5
    assert config.sender_type_boost_map.get("automated") == 0.3
    assert config.sender_type_boost_map.get("personal") == 0.8


def test_sender_type_boost_surfaces_exist():
    """Mutator exposes sender_type_boost surfaces."""
    from app.autoresearch.mutator import get_mutable_surfaces

    configs_dir = Path(__file__).resolve().parents[1] / "configs"
    surfaces = get_mutable_surfaces(configs_dir, surface_filter="retrieval")
    names = [s.name for s in surfaces]
    assert "sender_type_boost_external_client" in names
    assert "sender_type_boost_personal" in names
    assert "sender_type_boost_automated" in names
    assert "sender_type_boost_internal" in names


def test_sender_type_boost_surface_bounds():
    """Sender type boost surfaces have correct bounds."""
    from app.autoresearch.mutator import get_mutable_surfaces

    configs_dir = Path(__file__).resolve().parents[1] / "configs"
    surfaces = get_mutable_surfaces(configs_dir, surface_filter="retrieval")
    for s in surfaces:
        if s.name.startswith("sender_type_boost_"):
            assert s.step_size == 0.1
            assert s.min_val == 0.5
            assert s.max_val == 2.0


def test_sender_type_boost_mutation(tmp_path):
    """Sender type boost can be mutated and reverted."""
    from app.autoresearch.mutator import apply_mutation, get_mutable_surfaces, revert_mutation

    configs_dir = tmp_path / "configs"
    retrieval_dir = configs_dir / "retrieval"
    retrieval_dir.mkdir(parents=True)
    (retrieval_dir / "defaults.yaml").write_text(
        yaml.dump({
            "top_k_reply_pairs": 8,
            "top_k_chunks": 3,
            "recency_boost_days": 60,
            "recency_boost_weight": 0.2,
            "account_boost_weight": 0.15,
            "sender_type_boost_map": {
                "external_client": 1.3,
                "personal": 0.8,
                "automated": 0.3,
                "internal": 1.5,
            },
        })
    )
    prompts_path = configs_dir / "prompts.yaml"
    prompts_path.write_text(yaml.dump({"drafting_prompt": "test", "system_prompt": "test"}))

    surfaces = get_mutable_surfaces(configs_dir, surface_filter="retrieval")
    ext_surface = next(s for s in surfaces if s.name == "sender_type_boost_external_client")
    assert ext_surface.current_value == 1.3

    old = apply_mutation(ext_surface, configs_dir)
    assert old == 1.3
    # Should have incremented by 0.1
    assert abs(ext_surface.current_value - 1.4) < 0.01

    revert_mutation(ext_surface, old, configs_dir)
    assert abs(ext_surface.current_value - 1.3) < 0.01


# --- Item 6: Lower semantic reranking threshold ---


def test_semantic_min_coverage_default():
    """Default semantic_min_coverage is 0.01."""
    config = RetrievalConfig(
        top_k_documents=3, top_k_chunks=3, top_k_reply_pairs=5,
        recency_boost_days=60, recency_boost_weight=0.2,
        account_boost_weight=0.15, source_weights={},
    )
    assert config.semantic_min_coverage == 0.01


def test_semantic_min_coverage_from_config():
    """Config file now loads 0.01 threshold."""
    from app.retrieval.service import _load_retrieval_config

    configs_dir = Path(__file__).resolve().parents[1] / "configs"
    config = _load_retrieval_config(configs_dir)
    assert config.semantic_min_coverage == 0.01


def test_partial_semantic_coverage_flag():
    """RetrievalResponse tracks partial_semantic_coverage flag."""
    from app.retrieval.service import RetrievalResponse

    resp = RetrievalResponse(
        query="test", retrieval_method="fts5_bm25+semantic",
        semantic_search_enabled=True, applied_filters={},
        detected_mode="work", documents=[], chunks=[], reply_pairs=[],
        partial_semantic_coverage=True,
    )
    assert resp.partial_semantic_coverage is True
    d = resp.to_dict()
    assert d["partial_semantic_coverage"] is True


# --- Item 7: Cross-encoder reranking ---


def _make_match(score: float, snippet: str = "test") -> RetrievalMatch:
    return RetrievalMatch(
        result_type="reply_pair", score=score, lexical_score=score,
        metadata_score=0.0, source_type="gmail", source_id="1",
        account_email=None, title=None, author=None, external_uri=None,
        thread_id=None, created_at=None, updated_at=None,
        snippet=snippet,
    )


def test_reranker_graceful_fallback():
    """Reranker returns matches unchanged when sentence_transformers not available."""
    import app.core.reranker as reranker_mod

    # Reset state
    reranker_mod._cross_encoder = None
    reranker_mod._load_attempted = False

    with patch.dict("sys.modules", {"sentence_transformers": None}):
        reranker_mod._load_attempted = False
        reranker_mod._cross_encoder = None
        # Force ImportError path
        matches = [_make_match(5.0), _make_match(3.0)]
        result = reranker_mod.rerank("test query", matches, 2)
        assert result == matches


def test_reranker_with_mock_encoder():
    """Reranker reorders matches based on cross-encoder scores."""
    import app.core.reranker as reranker_mod

    mock_encoder = MagicMock()
    # Second match should score higher
    mock_encoder.predict.return_value = [0.2, 0.9]

    reranker_mod._cross_encoder = mock_encoder
    reranker_mod._load_attempted = True

    matches = [_make_match(5.0, "low relevance"), _make_match(3.0, "high relevance")]
    result = reranker_mod.rerank("test query", matches, 2)

    # Second match should come first after reranking
    assert result[0].snippet == "high relevance"

    # Cleanup
    reranker_mod._cross_encoder = None
    reranker_mod._load_attempted = False


def test_reranker_config_defaults():
    """RetrievalConfig has reranker_enabled=False by default."""
    config = RetrievalConfig(
        top_k_documents=3, top_k_chunks=3, top_k_reply_pairs=5,
        recency_boost_days=60, recency_boost_weight=0.2,
        account_boost_weight=0.15, source_weights={},
    )
    assert config.reranker_enabled is False
