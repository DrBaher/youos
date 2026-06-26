"""Config mutation engine for YouOS Autoresearch."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ConfigSurface:
    name: str  # e.g. "top_k_reply_pairs" or "drafting_prompt"
    config_file: str  # relative to configs_dir, e.g. "retrieval/defaults.yaml"
    yaml_key: str  # top-level key in the yaml file
    current_value: Any
    mutation_type: str  # "numeric_step" | "template_variant"
    step_size: float | None = None
    min_val: float | None = None
    max_val: float | None = None
    variants: list[Any] | None = None
    variant_index: int = 0  # for template_variant: which variant is current
    # Snapshot of the FULL composite_weights dict taken before a composite-weight
    # mutation, so revert can restore all three weights (normalization rewrites
    # all of them; reverting only the mutated key leaves the rest skewed).
    composite_snapshot: dict[str, float] | None = None


# -- Numeric surface definitions ────────────────────────────────────

_NUMERIC_SURFACES: list[dict[str, Any]] = [
    {"name": "top_k_reply_pairs", "config_file": "retrieval/defaults.yaml", "yaml_key": "top_k_reply_pairs", "step_size": 1, "min_val": 3, "max_val": 10},
    {"name": "top_k_chunks", "config_file": "retrieval/defaults.yaml", "yaml_key": "top_k_chunks", "step_size": 1, "min_val": 1, "max_val": 6},
    {"name": "recency_boost_days", "config_file": "retrieval/defaults.yaml", "yaml_key": "recency_boost_days", "step_size": 30, "min_val": 30, "max_val": 365},
    {
        "name": "recency_boost_weight",
        "config_file": "retrieval/defaults.yaml",
        "yaml_key": "recency_boost_weight",
        "step_size": 0.05,
        "min_val": 0.0,
        "max_val": 0.5,
    },
    {
        "name": "account_boost_weight",
        "config_file": "retrieval/defaults.yaml",
        "yaml_key": "account_boost_weight",
        "step_size": 0.05,
        "min_val": 0.0,
        "max_val": 0.4,
    },
    # E16: semantic vs keyword balance
    {
        "name": "semantic_weight",
        "config_file": "retrieval/defaults.yaml",
        "yaml_key": "semantic_weight",
        "step_size": 0.05,
        "min_val": 0.0,
        "max_val": 1.0,
    },
    # E16: exemplar display length tuning
    {
        "name": "exemplar_reply_chars",
        "config_file": "retrieval/defaults.yaml",
        "yaml_key": "exemplar_reply_chars",
        "step_size": 100,
        "min_val": 200,
        "max_val": 1000,
    },
    {
        "name": "exemplar_inbound_chars",
        "config_file": "retrieval/defaults.yaml",
        "yaml_key": "exemplar_inbound_chars",
        "step_size": 100,
        "min_val": 100,
        "max_val": 600,
    },
    # BM25 lexical-score saturation. Historically hardcoded
    # `min(raw_rank * 2.0, 10.0)` — strong matches above raw_rank=5 all
    # flattened to lexical_score=10 and lost their order. Surfacing the scale
    # and cap lets autoresearch's golden-eval-gated loop find the right
    # ramp/ceiling for this corpus instead of guessing.
    {
        "name": "lexical_scale",
        "config_file": "retrieval/defaults.yaml",
        "yaml_key": "lexical_scale",
        "step_size": 0.5,
        "min_val": 0.5,
        "max_val": 4.0,
    },
    {
        "name": "lexical_cap",
        "config_file": "retrieval/defaults.yaml",
        "yaml_key": "lexical_cap",
        "step_size": 2.0,
        "min_val": 6.0,
        "max_val": 20.0,
    },
    # Subject/title/topic/sender boosts. Each was already in scoring code
    # via RetrievalConfig but only `recency_boost_*` and `account_boost_*`
    # were exposed for autoresearch — every other lever stayed pinned at its
    # historical default. Opening them up to the same golden-eval-gated loop
    # lets the optimizer find the right balance for *this* corpus instead of
    # the original guesses.
    {
        "name": "subject_match_boost",
        "config_file": "retrieval/defaults.yaml",
        "yaml_key": "subject_match_boost",
        "step_size": 0.05,
        "min_val": 0.0,
        "max_val": 0.5,
    },
    {
        "name": "topic_match_boost",
        "config_file": "retrieval/defaults.yaml",
        "yaml_key": "topic_match_boost",
        "step_size": 0.05,
        "min_val": 0.0,
        "max_val": 0.5,
    },
    {
        "name": "sender_type_boost",
        "config_file": "retrieval/defaults.yaml",
        "yaml_key": "sender_type_boost",
        "step_size": 0.05,
        "min_val": 0.0,
        "max_val": 0.5,
    },
    {
        "name": "sender_domain_boost",
        "config_file": "retrieval/defaults.yaml",
        "yaml_key": "sender_domain_boost",
        "step_size": 0.05,
        "min_val": 0.0,
        "max_val": 0.4,
    },
    {
        "name": "field_match_bonus_per_token",
        "config_file": "retrieval/defaults.yaml",
        "yaml_key": "field_match_bonus_per_token",
        "step_size": 0.05,
        "min_val": 0.0,
        "max_val": 0.5,
    },
]

# E16: Per-mode avg_reply_words surfaces (nested in persona.yaml modes)
_MODE_AVG_WORDS_SURFACES: list[dict[str, Any]] = [
    {"name": f"mode_avg_words_{mode}", "sender_type": mode, "step_size": 10, "min_val": 20, "max_val": 200}
    for mode in ("external_client", "personal", "internal", "automated")
]

# Per-sender-type boost surfaces (nested under sender_type_boost_map)
_SENDER_TYPE_BOOST_SURFACES: list[dict[str, Any]] = [
    {"name": f"sender_type_boost_{st}", "sender_type": st, "step_size": 0.1, "min_val": 0.5, "max_val": 2.0}
    for st in ("external_client", "personal", "automated", "internal")
]

# -- Prompt variant definitions ─────────────────────────────────────

# Additive instruction A/B'd onto the system prompt via `system_prompt_suffix`
# (consumed in app/generation/service.assemble_prompt). These are appended, so
# they tune drafting STYLE without clobbering the instance's persona system
# prompt. variant_a is empty = the instance's prompt unchanged (the baseline).
_SYSTEM_PROMPT_SUFFIX_VARIANTS: list[str] = [
    # variant_a: no suffix (baseline — system prompt as-is)
    "",
    # variant_b: direct, answer-first
    "Be direct and concise: lead with the answer, skip pleasantries, and don't restate the question.",
    # variant_c: skimmable structure
    "Keep replies skimmable: short paragraphs, and bullet points for any multi-item answer. State the key point first.",
    # variant_d (b…): warm AND brief — targets the golden warmth + brevity
    # failures together (personal-warmth was too long AND missed warmth keywords).
    "Be warm and personable but brief: acknowledge the person in one short line, "
    "then get to the point in a few sentences. No filler, no over-explaining.",
    # variant_e: keyword-faithful + tight — echo the sender's own key terms and
    # keep it short (lifts keyword-hit without inflating length).
    "Answer using the sender's own key terms and specifics; keep it tight — a few "
    "sentences at most — and lead with the substantive point.",
]


_COMPOSITE_WEIGHT_SURFACES: list[dict[str, Any]] = [
    {"name": f"composite_weight_{k}", "yaml_key": f"composite_weights.{k}", "step_size": 0.05, "min_val": 0.0, "max_val": 1.0}
    for k in ("pass_rate", "avg_keyword_hit", "avg_confidence")
]


def _normalize_composite_weights(data: dict[str, Any]) -> None:
    """Enforce sum == 1.0 for composite weights after mutation."""
    weights = data.get("composite_weights", {})
    if not weights:
        return
    total = sum(float(v) for v in weights.values())
    if total > 0:
        for k in weights:
            weights[k] = round(float(weights[k]) / total, 4)


def get_mutable_surfaces(configs_dir: Path, *, surface_filter: str | None = None) -> list[ConfigSurface]:
    """Load current config values and return mutable surfaces."""
    surfaces: list[ConfigSurface] = []

    if surface_filter is None or surface_filter == "retrieval":
        retrieval_path = configs_dir / "retrieval" / "defaults.yaml"
        retrieval_data = yaml.safe_load(retrieval_path.read_text(encoding="utf-8")) or {}

        for spec in _NUMERIC_SURFACES:
            current = retrieval_data.get(spec["yaml_key"])
            if current is None:
                continue
            # Coerce to match step type
            if isinstance(spec["step_size"], int):
                current = int(current)
            else:
                current = float(current)
            surfaces.append(
                ConfigSurface(
                    name=spec["name"],
                    config_file=spec["config_file"],
                    yaml_key=spec["yaml_key"],
                    current_value=current,
                    mutation_type="numeric_step",
                    step_size=spec["step_size"],
                    min_val=spec["min_val"],
                    max_val=spec["max_val"],
                )
            )

        # Sender-type boost map surfaces
        boost_map = retrieval_data.get("sender_type_boost_map", {})
        for spec in _SENDER_TYPE_BOOST_SURFACES:
            st = spec["sender_type"]
            current = float(boost_map.get(st, 1.0))
            surfaces.append(
                ConfigSurface(
                    name=spec["name"],
                    config_file="retrieval/defaults.yaml",
                    yaml_key=f"sender_type_boost_map.{st}",
                    current_value=current,
                    mutation_type="numeric_step",
                    step_size=spec["step_size"],
                    min_val=spec["min_val"],
                    max_val=spec["max_val"],
                )
            )

    # E16: per-mode avg_reply_words from persona.yaml
    if surface_filter is None or surface_filter == "persona":
        persona_path = configs_dir / "persona.yaml"
        if persona_path.exists():
            persona_data = yaml.safe_load(persona_path.read_text(encoding="utf-8")) or {}
            modes = persona_data.get("modes", {})
            for spec in _MODE_AVG_WORDS_SURFACES:
                mode = spec["sender_type"]
                mode_cfg = modes.get(mode, {})
                current_words = mode_cfg.get("avg_reply_words")
                if current_words is None:
                    continue
                surfaces.append(
                    ConfigSurface(
                        name=spec["name"],
                        config_file="persona.yaml",
                        yaml_key=f"modes.{mode}.avg_reply_words",
                        current_value=int(current_words),
                        mutation_type="numeric_step",
                        step_size=spec["step_size"],
                        min_val=spec["min_val"],
                        max_val=spec["max_val"],
                    )
                )

    if surface_filter is None or surface_filter == "autoresearch":
        autoresearch_path = configs_dir / "autoresearch.yaml"
        if autoresearch_path.exists():
            ar_data = yaml.safe_load(autoresearch_path.read_text(encoding="utf-8")) or {}
            weights = ar_data.get("composite_weights", {})
            for spec in _COMPOSITE_WEIGHT_SURFACES:
                key_parts = spec["yaml_key"].split(".")
                current = float(weights.get(key_parts[-1], 0.0))
                surfaces.append(
                    ConfigSurface(
                        name=spec["name"],
                        config_file="autoresearch.yaml",
                        yaml_key=spec["yaml_key"],
                        current_value=current,
                        mutation_type="numeric_step",
                        step_size=spec["step_size"],
                        min_val=spec["min_val"],
                        max_val=spec["max_val"],
                    )
                )

    if surface_filter is None or surface_filter == "prompt_drafting":
        prompts_path = configs_dir / "prompts.yaml"
        prompts_data = yaml.safe_load(prompts_path.read_text(encoding="utf-8")) or {}
        # Mutate `system_prompt_suffix` — the key generation actually consumes
        # (the old `drafting_prompt` key was read by nothing, so this surface
        # was a guaranteed no-op).
        current_prompt = prompts_data.get("system_prompt_suffix", "") or ""

        variant_index = 0
        for i, variant in enumerate(_SYSTEM_PROMPT_SUFFIX_VARIANTS):
            if variant.strip() == current_prompt.strip():
                variant_index = i
                break

        surfaces.append(
            ConfigSurface(
                name="system_prompt_suffix",
                config_file="prompts.yaml",
                yaml_key="system_prompt_suffix",
                current_value=current_prompt,
                mutation_type="template_variant",
                variants=_SYSTEM_PROMPT_SUFFIX_VARIANTS,
                variant_index=variant_index,
            )
        )

    # Order so the surfaces that actually CHANGE THE DRAFT lead — the prompt
    # template and per-mode reply length — followed by retrieval, then the
    # composite-weight meta-surface. A short run (or the nightly's early
    # iterations) then exercises the high-headroom generation surfaces first,
    # rather than spending its whole budget on retrieval knobs that (as the
    # b78 diagnostic showed) rarely change which exemplars are selected.
    _priority = {"prompts.yaml": 0, "persona.yaml": 1, "retrieval/defaults.yaml": 2, "autoresearch.yaml": 3}
    surfaces.sort(key=lambda s: _priority.get(s.config_file, 9))
    return surfaces


def apply_mutation(surface: ConfigSurface, configs_dir: Path) -> Any:
    """Apply one mutation step to the surface. Returns old value for revert."""
    old_value = surface.current_value
    file_path = configs_dir / surface.config_file
    raw = file_path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}

    if surface.mutation_type == "numeric_step":
        new_value = _next_numeric_value(surface)
        if new_value == old_value:
            return old_value  # at boundary, no mutation possible
        is_composite = surface.config_file == "autoresearch.yaml" and "composite_weights" in surface.yaml_key
        if is_composite:
            # Snapshot ALL weights before mutating, so revert can restore them
            # (normalization below rewrites every weight, not just this one).
            surface.composite_snapshot = dict(data.get("composite_weights") or {})
        _set_nested(data, surface.yaml_key, new_value)
        surface.current_value = new_value
        # Normalize composite weights if this is an autoresearch weight
        if is_composite:
            _normalize_composite_weights(data)
    elif surface.mutation_type == "template_variant":
        new_index = (surface.variant_index + 1) % len(surface.variants)
        new_value = surface.variants[new_index]
        data[surface.yaml_key] = new_value
        surface.current_value = new_value
        surface.variant_index = new_index
    else:
        raise ValueError(f"Unknown mutation type: {surface.mutation_type}")

    _write_yaml(file_path, data)
    return old_value


def revert_mutation(surface: ConfigSurface, old_value: Any, configs_dir: Path) -> None:
    """Revert a mutation by restoring the old value exactly."""
    file_path = configs_dir / surface.config_file
    raw = file_path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    if surface.composite_snapshot is not None:
        # Restore the WHOLE composite_weights dict — normalization changed all
        # three, so restoring only the mutated key would leave the rest skewed
        # and the sum != 1, silently corrupting the eval objective over time.
        data["composite_weights"] = dict(surface.composite_snapshot)
        surface.current_value = old_value
        surface.composite_snapshot = None
    else:
        _set_nested(data, surface.yaml_key, old_value)
        surface.current_value = old_value
        # For template variants, find the matching index
        if surface.mutation_type == "template_variant" and surface.variants:
            for i, v in enumerate(surface.variants):
                if v.strip() == str(old_value).strip():
                    surface.variant_index = i
                    break
    _write_yaml(file_path, data)


def describe_mutation(surface: ConfigSurface) -> str:
    """Describe what the next mutation would do."""
    if surface.mutation_type == "numeric_step":
        new_val = _next_numeric_value(surface)
        if new_val == surface.current_value:
            return f"{surface.name}: {surface.current_value} (at boundary, skip)"
        return f"{surface.name}: {surface.current_value} -> {new_val}"
    elif surface.mutation_type == "template_variant":
        new_index = (surface.variant_index + 1) % len(surface.variants)
        labels = ["variant_a", "variant_b", "variant_c"]
        old_label = labels[surface.variant_index] if surface.variant_index < len(labels) else f"variant_{surface.variant_index}"
        new_label = labels[new_index] if new_index < len(labels) else f"variant_{new_index}"
        return f"{surface.name}: {old_label} -> {new_label}"
    return f"{surface.name}: unknown mutation"


def _next_numeric_value(surface: ConfigSurface) -> Any:
    """Compute the next value for a numeric surface (increment by step)."""
    new_val = surface.current_value + surface.step_size
    if surface.max_val is not None and new_val > surface.max_val:
        # Try decrementing instead
        alt = surface.current_value - surface.step_size
        if surface.min_val is not None and alt < surface.min_val:
            return surface.current_value  # at boundary
        return alt
    return new_val


def _set_nested(data: dict[str, Any], key: str, value: Any) -> None:
    """Set a value in a dict, supporting dotted keys like 'sender_type_boost_map.internal'."""
    parts = key.split(".")
    if len(parts) == 1:
        data[key] = value
    else:
        obj = data
        for part in parts[:-1]:
            obj = obj.setdefault(part, {})
        obj[parts[-1]] = value


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    """Write YAML preserving block scalar style for multi-line strings.

    Atomic: these config files are read live by the API server and by a
    concurrent autoresearch process, so a plain truncate-then-write exposes a
    torn read (an empty/partial YAML parsed as a broken config). Render fully,
    write to a temp file in the same directory, fsync, then os.replace (an
    atomic rename on the same filesystem).
    """
    import os
    import tempfile

    path = Path(path)
    rendered = yaml.dump(
        data, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120
    )
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(rendered)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
