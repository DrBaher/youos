"""More retrieval surfaces wired into autoresearch (#19 follow-up).

PR #19 opened ``lexical_scale``/``lexical_cap`` to the autoresearch loop.
This module pins five more retrieval levers that were already in
``RetrievalConfig`` and the scoring code but invisible to the optimizer —
so the loop had no way to find better values than the original guesses.

Same risk profile as PR #19: defaults match the historical hardcoded
constants exactly, so the change is zero-behaviour at YAML-silent or
default values. New mutation surfaces only.

Also fixes one latent bug found while doing the wiring: the
``subject_match_boost`` field was on ``RetrievalConfig`` with a default
of ``0.2`` but ``_load_retrieval_config`` never read it from YAML, so
the YAML value was silently ignored. Test pins the round-trip.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.retrieval.service import RetrievalConfig, _field_match_bonus, _load_retrieval_config


def _write_defaults_yaml(tmp_path: Path, extra: dict | None = None) -> Path:
    payload = {
        "top_k_reply_pairs": 8,
        "top_k_documents": 3,
        "top_k_chunks": 3,
        "recency_boost_days": 60,
        "recency_boost_weight": 0.2,
        "account_boost_weight": 0.15,
    }
    if extra:
        payload.update(extra)
    configs = tmp_path / "configs"
    (configs / "retrieval").mkdir(parents=True)
    (configs / "retrieval" / "defaults.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    return configs


# ── 1. RetrievalConfig defaults preserve historical constants ─────────────

def test_retrieval_config_defaults_for_new_boosts():
    cfg = RetrievalConfig(
        top_k_documents=3,
        top_k_chunks=3,
        top_k_reply_pairs=5,
        recency_boost_days=90,
        recency_boost_weight=0.2,
        account_boost_weight=0.15,
        source_weights={},
    )
    assert cfg.subject_match_boost == 0.2
    assert cfg.topic_match_boost == 0.15
    assert cfg.sender_type_boost == 0.15
    assert cfg.sender_domain_boost == 0.10
    assert cfg.field_match_bonus_per_token == 0.25


# ── 2. YAML loader reads the new keys ────────────────────────────────────

def test_yaml_loader_reads_new_keys(tmp_path):
    configs = _write_defaults_yaml(
        tmp_path,
        {
            "subject_match_boost": 0.35,
            "topic_match_boost": 0.25,
            "sender_type_boost": 0.2,
            "sender_domain_boost": 0.18,
            "field_match_bonus_per_token": 0.4,
        },
    )
    cfg = _load_retrieval_config(configs)
    assert cfg.subject_match_boost == 0.35
    assert cfg.topic_match_boost == 0.25
    assert cfg.sender_type_boost == 0.2
    assert cfg.sender_domain_boost == 0.18
    assert cfg.field_match_bonus_per_token == 0.4


def test_yaml_loader_subject_match_boost_was_dropped_before_this_pr(tmp_path):
    """Regression for the latent bug: pre-PR the dataclass had
    ``subject_match_boost`` but ``_load_retrieval_config`` never read the
    YAML value, so a YAML override was silently ignored."""
    configs = _write_defaults_yaml(tmp_path, {"subject_match_boost": 0.99})
    cfg = _load_retrieval_config(configs)
    assert cfg.subject_match_boost == 0.99


def test_yaml_silent_falls_back_to_dataclass_defaults(tmp_path):
    """An older instance's YAML (silent on the new keys) must still produce
    the historical scoring behaviour."""
    configs = _write_defaults_yaml(tmp_path)
    cfg = _load_retrieval_config(configs)
    assert cfg.subject_match_boost == 0.2
    assert cfg.topic_match_boost == 0.15
    assert cfg.sender_type_boost == 0.15
    assert cfg.sender_domain_boost == 0.10
    assert cfg.field_match_bonus_per_token == 0.25


# ── 3. _field_match_bonus honors the per-token multiplier ────────────────

def test_field_match_bonus_default_multiplier_matches_legacy():
    """Behaviour at the default keyword arg must reproduce the old hardcoded
    `matched * 0.25` for any token set."""
    text = "Q3 invoice question"
    tokens = ["invoice", "q3", "missing"]
    assert _field_match_bonus(text, tokens) == 0.5  # 2 matches * 0.25


def test_field_match_bonus_respects_multiplier_arg():
    text = "Q3 invoice question"
    tokens = ["invoice", "q3"]
    assert _field_match_bonus(text, tokens, multiplier=0.5) == 1.0
    assert _field_match_bonus(text, tokens, multiplier=0.0) == 0.0


def test_field_match_bonus_no_match_is_still_zero():
    assert _field_match_bonus("Q3 invoice", ["foo", "bar"], multiplier=10.0) == 0.0


# ── 4. Autoresearch exposes all five new surfaces ────────────────────────

EXPECTED_NEW_SURFACES = (
    "subject_match_boost",
    "topic_match_boost",
    "sender_type_boost",
    "sender_domain_boost",
    "field_match_bonus_per_token",
)


def test_autoresearch_exposes_new_retrieval_surfaces(tmp_path):
    configs = _write_defaults_yaml(
        tmp_path,
        {
            "subject_match_boost": 0.2,
            "topic_match_boost": 0.15,
            "sender_type_boost": 0.15,
            "sender_domain_boost": 0.10,
            "field_match_bonus_per_token": 0.25,
        },
    )
    from app.autoresearch.mutator import get_mutable_surfaces

    by_name = {s.name: s for s in get_mutable_surfaces(configs, surface_filter="retrieval")}
    for name in EXPECTED_NEW_SURFACES:
        assert name in by_name, f"autoresearch missing surface: {name}"
        s = by_name[name]
        assert s.mutation_type == "numeric_step"
        assert s.step_size == 0.05
        assert s.min_val >= 0.0
        assert s.max_val <= 0.5
        assert s.current_value == pytest.approx(by_name[name].current_value)


def test_autoresearch_skips_new_surfaces_when_yaml_silent(tmp_path):
    """Same back-compat principle as PR #19: an older `defaults.yaml`
    without these keys yields no new mutation surfaces — autoresearch
    doesn't try to tune what the config didn't surface."""
    configs = _write_defaults_yaml(tmp_path)  # silent on all five
    from app.autoresearch.mutator import get_mutable_surfaces

    names = {s.name for s in get_mutable_surfaces(configs, surface_filter="retrieval")}
    for name in EXPECTED_NEW_SURFACES:
        assert name not in names


# ── 5. The shipped defaults YAML exposes all five (so a fresh install ──
#       sees them immediately, like PR #19's lexical_scale/lexical_cap) ──

def test_shipped_defaults_yaml_exposes_all_new_surfaces():
    """Drives the fresh-install experience: a user who never edits the YAML
    still gets the surfaces wired so the autoresearch loop can find better
    values from day 1."""
    repo_yaml = Path(__file__).resolve().parents[1] / "configs" / "retrieval" / "defaults.yaml"
    payload = yaml.safe_load(repo_yaml.read_text(encoding="utf-8")) or {}
    for key in EXPECTED_NEW_SURFACES:
        assert key in payload, f"shipped defaults.yaml missing {key} — autoresearch surfaces won't be wired by default"
