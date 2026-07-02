#!/usr/bin/env python
"""Compare local BASE MODELS (not just backends) on your own mail.

The built-in `youos compare-models` compares engines (mlx/ollama/claude) using
the ONE configured MLX base. This wires several *different* MLX base models into
the same voice-match harness so you can decide whether to switch off
Qwen3-4B-2507.

Arms (label -> base model + whether to apply your voice LoRA):
  qwen3-4b+lora   your current PROD setup: config base + latest adapter (warm server)
  qwen3-4b-base   same base, NO adapter — isolates what the LoRA buys
  qwen3.5-4b-base mlx-community/Qwen3.5-4B-4bit, no adapter (drop-in successor)
  gemma4-12b-base mlx-community/gemma-4-12B-it-qat-4bit, no adapter (capability ceiling)

The candidate arms are base-only (no voice LoRA exists for them yet), so read
them as: "does a newer/bigger BASE + your retrieval beat the current base?" If a
base-only candidate approaches the LoRA'd baseline, that's a strong signal to
switch and retrain the LoRA on it.

Mechanics: each arm runs the real `generate_draft` pinned to backend=mlx. For
non-adapter arms we override the base model id (`service._get_base_model_id`) and
set `use_adapter=False`, which routes to the cold `mlx_lm generate --model <id>`
subprocess (the warm server only serves the configured base+adapter). Scoring is
voice-match (lexical + style + length + semantic) against the reply you actually
sent, via the existing `app.evaluation.model_compare` harness.

Usage:
  python scripts/compare_bases.py --limit 12 --arms qwen3-4b+lora,qwen3-4b-base,qwen3.5-4b-base
  python scripts/compare_bases.py --limit 12 --arms gemma4-12b-base   # run the heavy one alone
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.generation.service as svc  # noqa: E402
from app.core.embeddings import get_embedding  # noqa: E402
from app.core.settings import get_settings  # noqa: E402
from app.evaluation.model_compare import (  # noqa: E402
    compare_models,
    format_comparison,
    sample_reply_pairs,
)
from app.generation.verify import verify_draft  # noqa: E402

# Per-arm fabrication tally (voice-match can't see fabrication — the very thing a
# bigger base is supposed to fix — so we score it alongside via the b286 verifier).
FAB: dict[str, dict[str, int]] = {}
# Per-draft records for eyeballing (arm, inbound, draft, which flags fired).
DUMP: list[dict] = []

# label -> (base_model_id or None=use config base, use_adapter)
ARMS: dict[str, tuple[str | None, bool]] = {
    "qwen3-4b+lora": (None, True),
    "qwen3-4b-base": (None, False),
    "qwen3.5-4b-base": ("mlx-community/Qwen3.5-4B-4bit", False),
    "gemma4-12b-base": ("mlx-community/gemma-4-12B-it-qat-4bit", False),
}


def _make_generate_fn(configs_dir: Path, *, live: bool = False):
    """Build a generate_fn that reads the arm label from ``backend`` and pins the
    right base model + adapter for that arm.

    ``live=True`` runs the REAL autonomous pipeline (deterministic=False) so the
    b187 concise-retry length control fires — the fair test for a verbose base
    like Qwen3.5, whose length my deterministic evals artificially left untamed.
    """
    from app.core.config import get_base_model

    config_base = get_base_model()

    def gen(prompt, *, backend, database_url, configs_dir=configs_dir):
        base_override, use_adapter = ARMS[backend]
        target_base = base_override or config_base
        # Only the cold subprocess path reads _get_base_model_id (the warm server
        # serves the configured base+adapter). Override it for every arm anyway.
        svc._get_base_model_id = lambda: target_base  # type: ignore[assignment]
        resp = svc.generate_draft(
            svc.DraftRequest(
                inbound_message=prompt,
                use_local_model=True,
                backend_override="mlx",
                use_adapter=use_adapter,
                deterministic=not live,  # live=on → concise-retry + abstain run
                seed=13,
                interactive=False,
                no_cloud_fallback=True,  # never let an empty local draft become Claude
            ),
            database_url=database_url,
            configs_dir=configs_dir,
        )
        # Score fabrication too (b286 verifier) — the axis a bigger base fixes.
        t = FAB.setdefault(backend, {"n": 0, "flagged": 0, "fabrication": 0, "blocking": 0, "status": 0})
        try:
            vr = verify_draft(resp.draft, inbound=prompt, user_name=svc._signoff_name())
            t["n"] += 1
            if vr.blocking:
                t["blocking"] += 1
            if vr.fabrications:
                t["fabrication"] += 1
            if vr.status_claims:
                t["status"] += 1
            if vr.blocking or vr.fabrications or vr.status_claims:
                t["flagged"] += 1
            DUMP.append({
                "arm": backend, "inbound": prompt[:400], "draft": resp.draft,
                "words": len(resp.draft.split()),
                "fabrications": list(vr.fabrications), "blocking": list(vr.blocking),
                "status_claims": list(vr.status_claims),
            })
        except Exception:
            pass
        return {
            "draft": resp.draft,
            "model_used": resp.model_used,
            "detected_mode": resp.detected_mode,
            "confidence": resp.confidence,
        }

    return gen


def _format_fabrication(arms: list[str]) -> str:
    lines = ["", "Fabrication (b286 verifier — lower is better):",
             f" {'arm':<16} {'n':>3} | {'flagged%':>8} {'fabric':>6} {'block':>5} {'status':>6}"]
    lines.append("-" * 52)
    for a in arms:
        t = FAB.get(a)
        if not t or not t["n"]:
            lines.append(f" {a:<16}   0 |    (no drafts scored)")
            continue
        pct = round(100.0 * t["flagged"] / t["n"], 1)
        lines.append(f" {a:<16} {t['n']:>3} | {pct:>7.1f}% {t['fabrication']:>6} {t['blocking']:>5} {t['status']:>6}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=12, help="real reply pairs to compare on")
    ap.add_argument("--arms", default="qwen3-4b+lora,qwen3-4b-base,qwen3.5-4b-base",
                    help="comma-separated arm labels (see ARMS)")
    ap.add_argument("--seed", type=int, default=13, help="case-sampling seed (reproducible)")
    ap.add_argument("--no-semantic", action="store_true", help="skip the embedding-based semantic score")
    ap.add_argument("--timeout", type=int, default=900, help="per-draft mlx subprocess timeout (12B cold-loads are slow)")
    ap.add_argument("--pause-server", action="store_true",
                    help="stop the warm model server for the run (frees ~3GB — needed for the 12B on 16GB), resume after")
    ap.add_argument("--live", action="store_true",
                    help="run the REAL pipeline (deterministic=False) so b187 concise-retry length control fires (fair test for verbose bases)")
    args = ap.parse_args()

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        print(f"unknown arms: {unknown}\nvalid: {list(ARMS)}", file=sys.stderr)
        return 2

    # 12B cold-loads per draft blow past the default 120s; raise it for this run.
    svc.SUBPROCESS_TIMEOUT = args.timeout

    settings = get_settings()
    database_url = settings.database_url
    configs_dir = ROOT / "configs"

    cases = sample_reply_pairs(database_url, limit=args.limit, seed=args.seed)
    if not cases:
        print("No reply-pair cases found in this instance's DB.", file=sys.stderr)
        return 1

    # Pause the warm 4B server so a 12B cold-load doesn't OOM 16GB (mirrors what
    # finetune_lora does). Resumed in the finally. NB: base-only arms don't touch
    # the warm server anyway; the +lora arm would restart it, so don't mix them here.
    from app.core import model_server
    paused = False
    if args.pause_server and model_server.is_enabled():
        try:
            model_server.stop()
            paused = True
            print("paused warm model server (will resume after)", flush=True)
        except Exception as exc:
            print(f"(could not pause model server: {exc})", flush=True)

    try:
        print(f"Comparing {len(arms)} arms on {len(cases)} of your real replies: {arms}\n", flush=True)
        embed_fn = None if args.no_semantic else get_embedding
        result = compare_models(
            cases, arms,
            database_url=database_url, configs_dir=configs_dir,
            generate_fn=_make_generate_fn(configs_dir, live=args.live), embed_fn=embed_fn,
        )
    finally:
        if paused:
            try:
                model_server.ensure_running()
                print("resumed warm model server", flush=True)
            except Exception as exc:
                print(f"(could not resume model server: {exc})", flush=True)

    print(format_comparison(result))
    print(_format_fabrication(arms))

    out_dir = ROOT / "var" / "model_compare"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = result.run_at.replace(":", "").replace("-", "")[:15]
    out = out_dir / f"base_compare_{stamp}.json"
    out.write_text(json.dumps(result.to_dict(), indent=2))
    print(f"\nSaved: {out}")
    if DUMP:
        dump = out_dir / f"drafts_{stamp}.jsonl"
        dump.write_text("\n".join(json.dumps(r) for r in DUMP))
        print(f"Per-draft dump: {dump}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
