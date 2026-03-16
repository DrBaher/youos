"""Tests for configurable autoresearch composite weights (Item 10)."""

from __future__ import annotations

import yaml

from app.autoresearch.mutator import (
    _normalize_composite_weights,
    get_mutable_surfaces,
)
from app.autoresearch.scorer import (
    _DEFAULT_WEIGHTS,
    load_composite_weights,
    reset_weight_cache,
)


def test_default_weights():
    """Default weights sum to 1.0."""
    assert sum(_DEFAULT_WEIGHTS.values()) == 1.0


def test_load_composite_weights_from_config(tmp_path):
    """Loads weights from autoresearch.yaml."""
    reset_weight_cache()
    config_path = tmp_path / "autoresearch.yaml"
    config_path.write_text(yaml.dump({"composite_weights": {"pass_rate": 0.6, "avg_keyword_hit": 0.2, "avg_confidence": 0.2}}))
    weights = load_composite_weights(tmp_path)
    assert weights["pass_rate"] == 0.6
    assert weights["avg_keyword_hit"] == 0.2
    reset_weight_cache()


def test_load_composite_weights_missing_file(tmp_path):
    """Falls back to defaults when config file is missing."""
    reset_weight_cache()
    weights = load_composite_weights(tmp_path)
    assert weights == _DEFAULT_WEIGHTS
    reset_weight_cache()


def test_normalize_composite_weights():
    """Weights are normalized to sum to 1.0."""
    data = {"composite_weights": {"pass_rate": 0.6, "avg_keyword_hit": 0.3, "avg_confidence": 0.3}}
    _normalize_composite_weights(data)
    total = sum(data["composite_weights"].values())
    assert abs(total - 1.0) < 0.01


def test_get_mutable_surfaces_includes_composite(tmp_path):
    """Composite weight surfaces appear in mutable surfaces."""
    # Create minimal config files
    (tmp_path / "autoresearch.yaml").write_text(yaml.dump({"composite_weights": {"pass_rate": 0.5, "avg_keyword_hit": 0.3, "avg_confidence": 0.2}}))
    (tmp_path / "retrieval").mkdir()
    (tmp_path / "retrieval" / "defaults.yaml").write_text(
        yaml.dump(
            {
                "top_k_reply_pairs": 5,
                "top_k_chunks": 3,
                "recency_boost_days": 90,
                "recency_boost_weight": 0.2,
                "account_boost_weight": 0.15,
            }
        )
    )
    (tmp_path / "prompts.yaml").write_text(yaml.dump({"drafting_prompt": "test"}))

    surfaces = get_mutable_surfaces(tmp_path, surface_filter="autoresearch")
    names = [s.name for s in surfaces]
    assert "composite_weight_pass_rate" in names
    assert "composite_weight_avg_keyword_hit" in names
    assert "composite_weight_avg_confidence" in names

    # Check bounds
    for s in surfaces:
        assert s.step_size == 0.05
        assert s.min_val == 0.0
        assert s.max_val == 1.0
