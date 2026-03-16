"""Tests for retrieval improvements (Items 5-7, 12)."""

from __future__ import annotations

from pathlib import Path

import yaml


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
