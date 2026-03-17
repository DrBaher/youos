"""
BaherOS Autoresearch — 80-iteration optimization loop.
Follows program.md exactly.
"""
from __future__ import annotations

import copy
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.autoresearch.mutator import (
    ConfigSurface,
    _DRAFTING_PROMPT_VARIANTS,
    _NUMERIC_SURFACES,
    apply_mutation,
    get_mutable_surfaces,
    revert_mutation,
)
from app.autoresearch.scorer import Scorecard, compare_scorecards, scorecard_from_eval_result
from app.evaluation.service import EvalRequest, run_eval_suite
from app.generation.service import DraftRequest, generate_draft

DATABASE_URL = f"sqlite:///{ROOT / 'var' / 'baheros.db'}"
CONFIGS_DIR = ROOT / "configs"
LOG_FILE = ROOT / "autoresearch_log.md"
MAX_ITERATIONS = 80
IMPROVE_THRESHOLD = 0.02  # keep if improved by >= 0.02


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gen_fn(prompt_text: str, *, database_url: str, configs_dir: Path) -> dict[str, Any]:
    response = generate_draft(
        DraftRequest(inbound_message=prompt_text),
        database_url=database_url,
        configs_dir=configs_dir,
    )
    return {
        "draft": response.draft,
        "detected_mode": response.detected_mode,
        "confidence": response.confidence,
        "precedent_count": len(response.precedent_used),
    }


def run_eval(tag: str) -> Scorecard:
    result = run_eval_suite(
        EvalRequest(config_tag=tag),
        generate_fn=gen_fn,
        database_url=DATABASE_URL,
        configs_dir=CONFIGS_DIR,
        persist=False,
    )
    return scorecard_from_eval_result(result)


def append_log(text: str) -> None:
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(text + "\n")


def read_retrieval_config() -> dict[str, Any]:
    path = CONFIGS_DIR / "retrieval" / "defaults.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def write_retrieval_config(data: dict[str, Any]) -> None:
    path = CONFIGS_DIR / "retrieval" / "defaults.yaml"
    path.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )


def read_prompts_config() -> dict[str, Any]:
    path = CONFIGS_DIR / "prompts.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def write_prompts_config(data: dict[str, Any]) -> None:
    path = CONFIGS_DIR / "prompts.yaml"
    path.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )


# ── Numeric surface definitions ────────────────────────────────────
RETRIEVAL_SURFACES = [
    {"key": "top_k_reply_pairs",  "min": 3,   "max": 12,  "step": 1},
    {"key": "top_k_chunks",       "min": 1,   "max": 8,   "step": 1},
    {"key": "top_k_documents",    "min": 1,   "max": 6,   "step": 1},
    {"key": "recency_boost_days", "min": 30,  "max": 365, "step": 30},
    {"key": "recency_boost_weight", "min": 0.0, "max": 0.5, "step": 0.05},
    {"key": "account_boost_weight", "min": 0.0, "max": 0.4, "step": 0.05},
]
PROMPT_SURFACE_KEY = "drafting_prompt"

# Track current direction per numeric surface (1 = up, -1 = down)
surface_direction: dict[str, int] = {s["key"]: 1 for s in RETRIEVAL_SURFACES}


def pick_next_mutation(iteration: int, retrieval_data: dict, prompts_data: dict, best_composite: float) -> dict:
    """
    Pick a candidate mutation for this iteration.
    Cycles through retrieval surfaces in round-robin, then tries prompt variants periodically.
    Direction reverses when hitting a boundary or after a revert.
    """
    # Every 12th iteration try the prompt surface
    if iteration % 12 == 0:
        current_prompt = prompts_data.get(PROMPT_SURFACE_KEY, "")
        current_idx = 0
        for i, v in enumerate(_DRAFTING_PROMPT_VARIANTS):
            if v.strip() == current_prompt.strip():
                current_idx = i
                break
        next_idx = (current_idx + 1) % len(_DRAFTING_PROMPT_VARIANTS)
        labels = ["variant_a", "variant_b", "variant_c"]
        return {
            "surface": PROMPT_SURFACE_KEY,
            "type": "prompt",
            "old_value": current_prompt,
            "new_value": _DRAFTING_PROMPT_VARIANTS[next_idx],
            "old_label": labels[current_idx],
            "new_label": labels[next_idx],
        }

    # Otherwise cycle through retrieval surfaces
    surface_idx = (iteration // 1) % len(RETRIEVAL_SURFACES)
    spec = RETRIEVAL_SURFACES[surface_idx]
    key = spec["key"]
    current = retrieval_data.get(key)
    if current is None:
        # try next
        spec = RETRIEVAL_SURFACES[(surface_idx + 1) % len(RETRIEVAL_SURFACES)]
        key = spec["key"]
        current = retrieval_data.get(key)

    step = spec["step"]
    direction = surface_direction.get(key, 1)

    # Compute candidate
    if isinstance(step, int):
        current = int(current)
        candidate = current + direction * step
        if candidate > spec["max"]:
            # Flip direction
            surface_direction[key] = -1
            candidate = current - step
        elif candidate < spec["min"]:
            surface_direction[key] = 1
            candidate = current + step
    else:
        current = round(float(current), 4)
        candidate = round(current + direction * step, 4)
        if candidate > spec["max"]:
            surface_direction[key] = -1
            candidate = round(current - step, 4)
        elif candidate < spec["min"]:
            surface_direction[key] = 1
            candidate = round(current + step, 4)

    return {
        "surface": key,
        "type": "retrieval",
        "old_value": current,
        "new_value": candidate,
    }


def main() -> None:
    print(f"[{now_utc()}] Starting BaherOS Autoresearch — 80 iterations", flush=True)
    append_log(f"\n## Autoresearch Run — {now_utc()} (80-iteration run)")

    # ── Step 0: Establish baseline ──────────────────────────────────
    print(f"[{now_utc()}] Running baseline eval...", flush=True)
    baseline = run_eval(f"autoresearch_baseline")
    print(f"[{now_utc()}] Baseline composite: {baseline.composite:.4f} "
          f"(pass={baseline.pass_rate:.2f} kw={baseline.avg_keyword_hit:.2f} "
          f"conf={baseline.avg_confidence:.2f})", flush=True)
    append_log(
        f"\n## Iteration 0 — Baseline — {now_utc()}\n"
        f"- Surface: N/A\n"
        f"- Change: N/A\n"
        f"- Composite: {baseline.composite:.4f} "
        f"(Pass={round(baseline.pass_rate*15)}, avg_kw={baseline.avg_keyword_hit:.2f}, "
        f"avg_conf={baseline.avg_confidence:.2f})\n"
        f"- Outcome: BASELINE\n"
        f"- Notes: Fresh baseline for 80-iteration run"
    )

    best = baseline
    improvements_kept = 0
    kept_changes: list[str] = []

    # ── Main optimization loop ──────────────────────────────────────
    for iteration in range(1, MAX_ITERATIONS + 1):
        retrieval_data = read_retrieval_config()
        prompts_data = read_prompts_config()

        mutation = pick_next_mutation(iteration, retrieval_data, prompts_data, best.composite)
        surface_name = mutation["surface"]
        old_val = mutation["old_value"]
        new_val = mutation["new_value"]
        tag = f"autoresearch_{iteration}"

        print(f"[{now_utc()}] Iter {iteration}/{MAX_ITERATIONS}: "
              f"{surface_name} {old_val} → {new_val}", flush=True)

        # Apply mutation
        if mutation["type"] == "retrieval":
            retrieval_data[surface_name] = new_val
            write_retrieval_config(retrieval_data)
        else:
            prompts_data[PROMPT_SURFACE_KEY] = new_val
            write_prompts_config(prompts_data)

        # Run eval
        try:
            candidate = run_eval(tag)
        except Exception as exc:
            print(f"[{now_utc()}] Eval failed: {exc} — reverting", flush=True)
            if mutation["type"] == "retrieval":
                retrieval_data[surface_name] = old_val
                write_retrieval_config(retrieval_data)
            else:
                prompts_data[PROMPT_SURFACE_KEY] = old_val
                write_prompts_config(prompts_data)
            append_log(
                f"\n## Iteration {iteration} — {now_utc()}\n"
                f"- Surface: {surface_name}\n"
                f"- Change: {old_val} → {new_val}\n"
                f"- Composite: {best.composite:.4f} → ERROR\n"
                f"- Outcome: REVERTED\n"
                f"- Notes: eval error: {exc}"
            )
            # Flip direction on error too
            if surface_name in surface_direction:
                surface_direction[surface_name] *= -1
            continue

        outcome = compare_scorecards(best, candidate)
        kept = outcome == "improved"

        if kept:
            best = candidate
            improvements_kept += 1
            if mutation["type"] == "prompt":
                kept_changes.append(
                    f"drafting_prompt: {mutation['old_label']} → {mutation['new_label']}"
                )
            else:
                kept_changes.append(f"{surface_name}: {old_val} → {new_val}")
            print(f"[{now_utc()}] KEPT — composite {candidate.composite:.4f}", flush=True)
        else:
            # Revert
            if mutation["type"] == "retrieval":
                retrieval_data[surface_name] = old_val
                write_retrieval_config(retrieval_data)
            else:
                prompts_data[PROMPT_SURFACE_KEY] = old_val
                write_prompts_config(prompts_data)
            # Flip direction for next attempt on this surface
            if surface_name in surface_direction:
                surface_direction[surface_name] *= -1
            print(f"[{now_utc()}] {outcome.upper()} — composite {candidate.composite:.4f} vs "
                  f"baseline {best.composite:.4f} — reverted", flush=True)

        # Log iteration
        if mutation["type"] == "prompt":
            change_desc = f"{mutation['old_label']} → {mutation['new_label']}"
        else:
            change_desc = f"{old_val} → {new_val}"

        append_log(
            f"\n## Iteration {iteration} — {now_utc()}\n"
            f"- Surface: {surface_name}\n"
            f"- Change: {change_desc}\n"
            f"- Composite: {best.composite if not kept else candidate.composite:.4f} "
            f"→ {candidate.composite:.4f}\n"
            f"- Outcome: {'KEPT' if kept else 'REVERTED'}\n"
            f"- Notes: pass={round(candidate.pass_rate*15)} "
            f"kw={candidate.avg_keyword_hit:.2f} conf={candidate.avg_confidence:.2f} "
            f"outcome={outcome}"
        )

    # ── Final summary ───────────────────────────────────────────────
    print(f"\n[{now_utc()}] DONE. {improvements_kept} improvements kept. "
          f"Final composite: {best.composite:.4f}", flush=True)

    changes_list = "\n".join(f"{i+1}. {c}" for i, c in enumerate(kept_changes)) or "None"
    final_retrieval = read_retrieval_config()

    append_log(
        f"\n---\n"
        f"\n## Final Summary — {now_utc()} (80-iteration run)\n\n"
        f"- **Iterations completed:** 80\n"
        f"- **Improvements kept:** {improvements_kept}\n"
        f"- **Baseline composite:** {baseline.composite:.4f}\n"
        f"- **Final composite:** {best.composite:.4f}\n"
        f"- **Total improvement:** +{best.composite - baseline.composite:.4f}\n\n"
        f"### Changes retained:\n{changes_list}\n\n"
        f"### Final config state:\n"
        f"- top_k_reply_pairs: {final_retrieval.get('top_k_reply_pairs')}\n"
        f"- top_k_documents: {final_retrieval.get('top_k_documents')}\n"
        f"- top_k_chunks: {final_retrieval.get('top_k_chunks')}\n"
        f"- recency_boost_days: {final_retrieval.get('recency_boost_days')}\n"
        f"- recency_boost_weight: {final_retrieval.get('recency_boost_weight')}\n"
        f"- account_boost_weight: {final_retrieval.get('account_boost_weight')}\n"
    )

    return improvements_kept, best.composite


if __name__ == "__main__":
    result = main()
    kept, final_score = result if isinstance(result, tuple) else (0, 0.0)
    print(f"\nFinal: {kept} improvements kept, composite {final_score:.4f}")
