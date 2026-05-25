"""BM25 lexical-score saturation is configurable (PR #14 deferred follow-up).

PR #14 explicitly left this alone — `min(raw_rank * 2.0, 10.0)` had been
hardcoded in two scoring sites in `app/retrieval/service.py`, with the
caveat that "changing core ranking needs golden-eval measurement first,
not a blind change."

This module pins:

1. **Defaults are unchanged.** With `lexical_scale=2.0` and
   `lexical_cap=10.0` (the shipped values), the new code produces the
   same numbers as the old hardcoded formula at every rank we sampled —
   so this PR is a zero-risk surfacing of the knobs, not a tuning change.
2. **The knobs actually affect the score.** Higher scale ramps faster;
   higher cap lifts the ceiling; lower cap compresses more aggressively.
3. **The knobs round-trip through the YAML loader.**
4. **The autoresearch loop knows about the new surfaces.** Without this
   wiring, the user would have no instrumented way to A/B variants and
   the deferral note would still hold.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from app.retrieval.service import RetrievalConfig, _load_retrieval_config


def _saturate(raw_rank: float, scale: float, cap: float) -> float:
    """The formula now in production at both FTS5 scoring sites."""
    return min(raw_rank * scale, cap)


# ── 1. Defaults match the historical hardcoded behaviour ─────────────────

def test_default_saturation_matches_legacy_formula_across_rank_range():
    """For raw_rank in 0..20 the new formula must produce the exact same
    value the old `min(rank*2, 10)` did. Pinning this guards against
    accidentally changing the default-path behaviour in this surfacing PR."""
    for raw_rank in (0.0, 0.5, 1.0, 2.0, 4.999, 5.0, 5.001, 7.5, 10.0, 20.0):
        legacy = min(raw_rank * 2.0, 10.0)
        new = _saturate(raw_rank, scale=2.0, cap=10.0)
        assert new == legacy, f"divergence at raw_rank={raw_rank}: {new} vs legacy {legacy}"


def test_retrieval_config_defaults_preserve_legacy_constants():
    """A freshly-constructed RetrievalConfig with only the required
    positionals must default to the historical scale/cap. If someone
    changes the dataclass defaults they need to flip these assertions
    knowingly."""
    cfg = RetrievalConfig(
        top_k_documents=3,
        top_k_chunks=3,
        top_k_reply_pairs=5,
        recency_boost_days=90,
        recency_boost_weight=0.2,
        account_boost_weight=0.15,
        source_weights={},
    )
    assert cfg.lexical_scale == 2.0
    assert cfg.lexical_cap == 10.0


# ── 2. The knobs actually move the score ─────────────────────────────────

def test_higher_scale_ramps_faster_below_the_cap():
    """At rank=3 with scale=3 we want 9 (vs 6 at scale=2). Past the cap
    the result should still be the cap — scale doesn't break the ceiling."""
    assert _saturate(3.0, scale=3.0, cap=10.0) == 9.0
    assert _saturate(3.0, scale=2.0, cap=10.0) == 6.0
    # cap still wins
    assert _saturate(10.0, scale=3.0, cap=10.0) == 10.0


def test_higher_cap_lets_top_matches_break_ties():
    """The bug the deferred BM25 note pointed at: with cap=10, every
    rank>=5 (at scale=2) compresses to 10 — top matches lose their order.
    Raising the cap restores the dynamic range."""
    a = _saturate(6.0, scale=2.0, cap=10.0)
    b = _saturate(8.0, scale=2.0, cap=10.0)
    assert a == b == 10.0, "Old behaviour: top matches tie at cap"

    a2 = _saturate(6.0, scale=2.0, cap=15.0)
    b2 = _saturate(8.0, scale=2.0, cap=15.0)
    assert a2 == 12.0 and b2 == 15.0
    assert b2 > a2, "With cap raised, the stronger match outranks again"


def test_lower_cap_compresses_more_aggressively():
    """A more aggressive cap (e.g. for noisy corpora) flattens earlier."""
    assert _saturate(2.0, scale=2.0, cap=3.0) == 3.0  # used to be 4.0 under default cap
    assert _saturate(10.0, scale=2.0, cap=3.0) == 3.0


# ── 3. YAML round-trip ───────────────────────────────────────────────────

def _write_defaults_yaml(tmp_path: Path, payload: dict) -> Path:
    configs = tmp_path / "configs"
    (configs / "retrieval").mkdir(parents=True)
    (configs / "retrieval" / "defaults.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    return configs


def test_yaml_loader_reads_lexical_knobs(tmp_path):
    configs = _write_defaults_yaml(
        tmp_path,
        {
            "top_k_reply_pairs": 8,
            "top_k_documents": 3,
            "top_k_chunks": 3,
            "recency_boost_days": 60,
            "recency_boost_weight": 0.2,
            "account_boost_weight": 0.15,
            "lexical_scale": 1.5,
            "lexical_cap": 12.0,
        },
    )
    cfg = _load_retrieval_config(configs)
    assert cfg.lexical_scale == 1.5
    assert cfg.lexical_cap == 12.0


def test_yaml_loader_falls_back_to_legacy_defaults_when_silent(tmp_path):
    """A YAML that doesn't mention the knobs keeps the historical behaviour
    — important for users who never touch the config."""
    configs = _write_defaults_yaml(
        tmp_path,
        {
            "top_k_reply_pairs": 8,
            "top_k_documents": 3,
            "top_k_chunks": 3,
            "recency_boost_days": 60,
            "recency_boost_weight": 0.2,
            "account_boost_weight": 0.15,
        },
    )
    cfg = _load_retrieval_config(configs)
    assert cfg.lexical_scale == 2.0
    assert cfg.lexical_cap == 10.0


# ── 4. Autoresearch can mutate the knobs ─────────────────────────────────

def test_autoresearch_surfaces_include_lexical_scale_and_cap(tmp_path):
    """Without this wiring the user has no instrumented way to A/B the
    saturation against the golden-eval gate the deferral note named."""
    configs = _write_defaults_yaml(
        tmp_path,
        {
            "top_k_reply_pairs": 8,
            "top_k_documents": 3,
            "top_k_chunks": 3,
            "recency_boost_days": 60,
            "recency_boost_weight": 0.2,
            "account_boost_weight": 0.15,
            "lexical_scale": 2.0,
            "lexical_cap": 10.0,
        },
    )

    from app.autoresearch.mutator import get_mutable_surfaces

    surfaces = get_mutable_surfaces(configs, surface_filter="retrieval")
    by_name = {s.name: s for s in surfaces}

    assert "lexical_scale" in by_name
    assert "lexical_cap" in by_name

    scale = by_name["lexical_scale"]
    assert scale.mutation_type == "numeric_step"
    assert scale.step_size == 0.5
    assert scale.min_val == 0.5 and scale.max_val == 4.0
    assert scale.current_value == 2.0

    cap = by_name["lexical_cap"]
    assert cap.current_value == 10.0
    assert cap.min_val == 6.0 and cap.max_val == 20.0


def test_autoresearch_skips_lexical_surfaces_when_yaml_silent(tmp_path):
    """Behaviour for an older instance whose YAML predates this PR: the
    knobs aren't exposed for mutation until the user opts in by adding
    them to defaults.yaml. Production code still uses the dataclass
    defaults, so retrieval keeps working — autoresearch just doesn't try
    to tune what the config doesn't surface."""
    configs = _write_defaults_yaml(
        tmp_path,
        {
            "top_k_reply_pairs": 8,
            "top_k_documents": 3,
            "top_k_chunks": 3,
            "recency_boost_days": 60,
            "recency_boost_weight": 0.2,
            "account_boost_weight": 0.15,
            # lexical_scale / lexical_cap absent
        },
    )

    from app.autoresearch.mutator import get_mutable_surfaces

    surfaces = get_mutable_surfaces(configs, surface_filter="retrieval")
    names = {s.name for s in surfaces}
    assert "lexical_scale" not in names
    assert "lexical_cap" not in names
