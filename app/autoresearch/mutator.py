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
    },  # noqa: E501
    {
        "name": "account_boost_weight",
        "config_file": "retrieval/defaults.yaml",
        "yaml_key": "account_boost_weight",
        "step_size": 0.05,
        "min_val": 0.0,
        "max_val": 0.4,
    },  # noqa: E501
]

# Per-sender-type boost surfaces (nested under sender_type_boost_map)
_SENDER_TYPE_BOOST_SURFACES: list[dict[str, Any]] = [
    {"name": f"sender_type_boost_{st}", "sender_type": st, "step_size": 0.1, "min_val": 0.5, "max_val": 2.0}
    for st in ("external_client", "personal", "automated", "internal")
]

# -- Prompt variant definitions ─────────────────────────────────────

_DRAFTING_PROMPT_VARIANTS: list[str] = [
    # variant_a: current standard
    """\
Task: produce a draft in your style grounded in retrieved corpus evidence.

Inputs:
- user request
- retrieved chunks
- relevant reply-pair exemplars

Requirements:
- preserve intent and tone consistent with your style
- do not invent personal facts that are not retrieved
- prefer concise drafts unless the request demands detail
""",
    # variant_b: concise format
    """\
Task: draft a concise reply in your style using retrieved evidence.

Inputs: user request, retrieved chunks, reply-pair exemplars.

Rules:
- match your style and intent
- no invented facts
- keep it short
""",
    # variant_c: direct, skip pleasantries
    """\
Task: produce a draft in your style grounded in retrieved corpus evidence.

Inputs:
- user request
- retrieved chunks
- relevant reply-pair exemplars

Requirements:
- preserve intent and tone consistent with your style
- do not invent personal facts that are not retrieved
- prefer concise drafts unless the request demands detail
- be direct, skip pleasantries
""",
]


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

    if surface_filter is None or surface_filter == "prompt_drafting":
        prompts_path = configs_dir / "prompts.yaml"
        prompts_data = yaml.safe_load(prompts_path.read_text(encoding="utf-8")) or {}
        current_prompt = prompts_data.get("drafting_prompt", "")

        # Determine which variant index matches current
        variant_index = 0
        for i, variant in enumerate(_DRAFTING_PROMPT_VARIANTS):
            if variant.strip() == current_prompt.strip():
                variant_index = i
                break

        surfaces.append(
            ConfigSurface(
                name="drafting_prompt",
                config_file="prompts.yaml",
                yaml_key="drafting_prompt",
                current_value=current_prompt,
                mutation_type="template_variant",
                variants=_DRAFTING_PROMPT_VARIANTS,
                variant_index=variant_index,
            )
        )

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
        _set_nested(data, surface.yaml_key, new_value)
        surface.current_value = new_value
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
    """Write YAML preserving block scalar style for multi-line strings."""
    path.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
