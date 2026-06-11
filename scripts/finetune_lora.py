"""LoRA fine-tuning script using mlx_lm on exported feedback pairs."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import get_base_model
from app.core.settings import get_adapter_path, get_settings
from app.db.bootstrap import resolve_sqlite_path

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT_DIR / "data" / "feedback"
BASE_MODEL = get_base_model()


def parse_args() -> argparse.Namespace:
    # Resolved lazily so YOUOS_DATA_DIR set in the calling shell lands the
    # adapter (and DB read) in the active instance, not the repo root —
    # the nightly invokes this script as a subprocess without --db / --adapter-dir.
    default_adapter_dir = get_adapter_path()
    default_db = resolve_sqlite_path(get_settings().database_url)
    p = argparse.ArgumentParser(description="LoRA fine-tuning with mlx_lm")
    p.add_argument("--iters", type=int, default=None, help="Training iterations (overrides auto-scaling)")
    p.add_argument("--num-layers", type=int, default=None, help="Number of LoRA layers (overrides auto-scaling)")
    p.add_argument("--learning-rate", type=float, default=None, help="Learning rate (overrides auto-scaling)")
    p.add_argument("--auto", action=argparse.BooleanOptionalAction, default=True, help="Auto-scale hyperparameters (default: True)")
    p.add_argument("--data-dir", type=str, default=str(DEFAULT_DATA_DIR), help="Directory with train.jsonl/valid.jsonl")
    p.add_argument("--adapter-dir", type=str, default=str(default_adapter_dir), help="Output adapter directory")
    p.add_argument("--db", type=str, default=str(default_db), help="Database path")
    p.add_argument("--dry-run", action="store_true", help="Show config without training")
    p.add_argument("--dpo", action="store_true", help="Use DPO training with data/dpo_train.jsonl")
    p.add_argument(
        "--persona",
        type=str,
        default=None,
        help=(
            "Train a per-persona adapter for the given sender_type cohort "
            "(e.g. --persona internal). When set, the adapter lands at "
            "<models>/adapters/personas/<persona>/ instead of the global "
            "<models>/adapters/latest/, and the post-train "
            "`used_in_finetune=1` marking is skipped (the global adapter "
            "still needs those rows). Used by Phase 2 of the per-persona "
            "adapters work."
        ),
    )
    return p.parse_args()


def count_jsonl_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with open(path, encoding="utf-8") as f:
        return sum(1 for _ in f)


def strip_curriculum_line(train_path: Path) -> bool:
    """Remove the leading ``{"_curriculum": ...}`` annotation line if present.

    The exporter prepends a metadata line recording the warmup split. mlx_lm
    (>=0.31) treats *every* JSONL line as a training record and rejects this one
    ("Unsupported data format"), aborting training on the very first line. The
    curriculum ordering (warmup examples first) lives in the row order, not this
    annotation, so dropping it preserves the curriculum benefit. Idempotent;
    returns True if a line was stripped.
    """
    if not train_path.exists():
        return False
    with open(train_path, encoding="utf-8") as f:
        lines = f.readlines()
    if lines and '"_curriculum"' in lines[0]:
        # Atomic (b241): an in-place rewrite re-opened the exact hole the
        # b163 exporter closed — a crash mid-write leaves a torn train.jsonl
        # the next finetune run trains on (then retires the pairs).
        from app.core.atomic_io import atomic_write_text

        atomic_write_text(train_path, "".join(lines[1:]))
        return True
    return False


def compute_auto_config(train_count: int) -> dict[str, int | float]:
    """Compute auto-scaled hyperparameters based on training set size."""
    iters = min(300, max(50, train_count * 3))
    num_layers = 16 if train_count >= 100 else 8
    learning_rate = 5e-5 if train_count < 20 else 1e-5
    return {"iters": iters, "num_layers": num_layers, "learning_rate": learning_rate}


# b176: emails are short; a 1024-token cap is plenty and slashes the activation
# working set. Used as the default --max-seq-length for memory-constrained
# (>=4B) tiers. The manual Qwen3-4B retrain that fit 16 GB used exactly this.
MAX_SEQ_LENGTH_DEFAULT = 1024


def _parse_model_size_b(base_model: str) -> float | None:
    """Extract the parameter count in billions from a model id (b176).

    Matches the first ``<num>B`` token (e.g. "Qwen3-4B" -> 4.0,
    "Llama-3.1-8B" -> 8.0, "Qwen2.5-1.5B" -> 1.5). Crucially, a trailing
    quantization tag like "-4bit" must NOT be read as 4 billion params, so the
    ``B`` must not be immediately followed by another letter. Returns None when
    no size token is present so callers fall back to a conservative default.
    """
    if not base_model:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z])", base_model)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _system_mem_gb() -> float | None:
    """Best-effort physical RAM in GiB via ``sysctl hw.memsize`` (b176)."""
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            check=False,
        )
        val = out.stdout.strip()
        if val:
            return int(val) / (1024 ** 3)
    except Exception:
        pass
    return None


def compute_memory_config(
    base_model: str,
    sys_mem_gb: float | None = None,
) -> dict:
    """Pick memory-lean knobs from base-model size and available RAM (b176).

    This is the *memory dimension* layered onto compute_auto_config()'s
    data-size dimension. The b174 migration moved the base to a 4B-4bit model;
    on a 16 GB M4 the prior flat 16-layer LoRA train with no seq cap and no
    grad-checkpointing blows past the ~12.7 GB GPU working set and OOMs, so the
    scheduled nightly retrain fails. The manual run that succeeded used
    --grad-checkpoint + --max-seq-length 1024 + --num-layers 8 (peak 3.97 GB).

    Returns:
      - grad_checkpoint: bool   trade compute for activation memory
      - max_seq_length:  int    cap on tokens per example (emails are short)
      - num_layers_cap:  int|None ceiling on trainable LoRA layers
      - batch_size_cap:  int|None ceiling on batch size

    A small (<2B) base is left close to the prior behavior (no grad-checkpoint,
    no caps) so the cheap path is not needlessly hobbled. Unknown size assumes
    the constrained default-base (4B) tier. Callers merge this onto
    compute_auto_config() and then apply explicit model.finetune.* overrides.
    """
    size_b = _parse_model_size_b(base_model)
    # If the size is unknown, assume the constrained default-base tier (4B).
    effective_b = size_b if size_b is not None else 4.0

    if effective_b < 2.0:
        # Small base (e.g. 1.5B): cheap to train, keep prior behavior.
        cfg = {
            "grad_checkpoint": False,
            "max_seq_length": 2048,
            "num_layers_cap": None,
            "batch_size_cap": None,
        }
    elif effective_b < 6.0:
        # ~4B: the known-good 16 GB recipe.
        cfg = {
            "grad_checkpoint": True,
            "max_seq_length": MAX_SEQ_LENGTH_DEFAULT,
            "num_layers_cap": 8,
            "batch_size_cap": 1,
        }
    else:
        # >=8B: tighter still.
        cfg = {
            "grad_checkpoint": True,
            "max_seq_length": MAX_SEQ_LENGTH_DEFAULT,
            "num_layers_cap": 4,
            "batch_size_cap": 1,
        }

    # On a tight-RAM box (<24 GB) force grad-checkpoint + batch cap even for a
    # mid base; the small tier stays relaxed (it fits comfortably).
    if sys_mem_gb is not None and sys_mem_gb < 24.0 and effective_b >= 2.0:
        cfg["grad_checkpoint"] = True
        cfg["batch_size_cap"] = 1
        if cfg["max_seq_length"] is None or cfg["max_seq_length"] > MAX_SEQ_LENGTH_DEFAULT:
            cfg["max_seq_length"] = MAX_SEQ_LENGTH_DEFAULT

    cfg["model_size_b"] = size_b
    cfg["sys_mem_gb"] = sys_mem_gb
    return cfg


def _finetune_overrides() -> dict:
    """Read optional model.finetune.* overrides from config (b176).

    Recognised keys: grad_checkpoint (bool), max_seq_length (int),
    num_layers (int), batch_size (int). Any present value wins over the
    auto-derived memory/data config. Best-effort: a missing/broken config
    just yields no overrides.
    """
    try:
        from app.core.config import load_config

        cfg = load_config() or {}
        model_cfg = cfg.get("model", {}) or {}
        return dict(model_cfg.get("finetune", {}) or {})
    except Exception:
        return {}


def resolve_train_knobs(
    base_model: str,
    auto: dict,
    iters_override: int | None = None,
    num_layers_override: int | None = None,
    learning_rate_override: float | None = None,
    overrides: dict | None = None,
    sys_mem_gb: float | None = None,
) -> dict:
    """Merge data-size config + memory config + explicit overrides (b176).

    Precedence (highest first): explicit CLI flag (iters/num-layers/lr) ->
    config model.finetune.* override -> memory cap (model-size/RAM) ->
    compute_auto_config() data-size pick. Returns the final OOM-safe knob set:
    iters, num_layers, learning_rate, batch_size, max_seq_length,
    grad_checkpoint.
    """
    if overrides is None:
        overrides = _finetune_overrides()

    iters = iters_override if iters_override is not None else auto["iters"]
    learning_rate = (
        learning_rate_override
        if learning_rate_override is not None
        else auto["learning_rate"]
    )
    num_layers = auto["num_layers"]
    batch_size = 1  # the prior fixed minibatch size

    mem = compute_memory_config(base_model, sys_mem_gb)

    grad_checkpoint = bool(overrides.get("grad_checkpoint", mem["grad_checkpoint"]))

    max_seq_length = overrides.get("max_seq_length", mem["max_seq_length"])
    if max_seq_length is not None:
        max_seq_length = int(max_seq_length)

    # num_layers: explicit CLI flag wins; else config override; else take the
    # smaller of the data-size pick and the model-size memory cap.
    if num_layers_override is not None:
        num_layers = int(num_layers_override)
    elif "num_layers" in overrides:
        num_layers = int(overrides["num_layers"])
    elif mem["num_layers_cap"] is not None:
        num_layers = min(num_layers, mem["num_layers_cap"])

    # batch_size: config override wins; else apply the memory cap.
    if "batch_size" in overrides:
        batch_size = int(overrides["batch_size"])
    elif mem["batch_size_cap"] is not None:
        batch_size = min(batch_size, mem["batch_size_cap"])

    return {
        "iters": iters,
        "num_layers": num_layers,
        "learning_rate": learning_rate,
        "batch_size": batch_size,
        "max_seq_length": max_seq_length,
        "grad_checkpoint": grad_checkpoint,
    }


def _promote_adapter(staging_dir: Path, adapter_dir: Path) -> bool:
    """Atomically promote a freshly-trained adapter from ``staging_dir`` into the
    live ``adapter_dir`` (b163).

    Validates ``adapters.safetensors`` before promoting so a half-written /
    corrupt train never lands in the served dir, then ``os.replace``s it (atomic
    within a filesystem) so the served file flips old-complete → new-complete with
    no truncated window. Returns True on success."""
    from app.core.model_server import _safetensors_ok

    staged = staging_dir / "adapters.safetensors"
    if not staged.exists() or not _safetensors_ok(staged):
        return False
    adapter_dir.mkdir(parents=True, exist_ok=True)
    os.replace(staged, adapter_dir / "adapters.safetensors")
    cfg = staging_dir / "adapter_config.json"
    if cfg.exists():
        os.replace(cfg, adapter_dir / "adapter_config.json")
    return True


# Outcome sentinels returned by run_training so main() can pick an exit code.
#   SUCCESS -> a fresh adapter was trained and promoted; exit 0.
#   SKIP    -> nothing to do this run (no/too-little fresh data, or --dry-run);
#             exit 0 so the nightly does not alarm on quiet nights. This mirrors
#             nightly_pipeline.should_skip_finetune, which already filters the
#             no-data / too-few-pairs case BEFORE this script is invoked.
#   FAILURE -> a run that was supposed to produce an adapter genuinely broke
#             (mlx returned nonzero, or the train produced no/corrupt adapter so
#             promotion failed); exit 1 so the failure is observable instead of
#             being silently reported as "[OK] finetune: true".
RESULT_SUCCESS = "success"
RESULT_SKIP = "skip"
RESULT_FAILURE = "failure"


def run_training(args: argparse.Namespace) -> str:
    data_dir = Path(args.data_dir)
    adapter_dir = Path(args.adapter_dir)
    train_path = data_dir / "train.jsonl"
    valid_path = data_dir / "valid.jsonl"

    train_count = count_jsonl_lines(train_path)
    valid_count = count_jsonl_lines(valid_path)

    # Detect, report, and strip curriculum metadata. mlx_lm rejects the
    # annotation line as a bad record (see strip_curriculum_line), so we must
    # remove it before training — not just discount it from the count.
    if train_path.exists():
        with open(train_path, encoding="utf-8") as f:
            first_line = f.readline().strip()
        if first_line and '"_curriculum"' in first_line:
            try:
                meta = json.loads(first_line)
                print(f"Curriculum learning detected: warmup={meta.get('warmup_count')}, total={meta.get('total')}")
            except json.JSONDecodeError:
                pass
            if strip_curriculum_line(train_path):
                train_count -= 1  # metadata line removed; no longer present

    # Determine hyperparameters. The data-size dimension (iters/num_layers/lr)
    # comes from compute_auto_config; b176 layers on a memory dimension
    # (grad-checkpoint, max-seq-length, num_layers/batch caps) driven by the
    # base-model SIZE and available RAM so a 4B/8B base fits a 16 GB M4 budget
    # without OOMing the nightly. Explicit CLI flags and model.finetune.*
    # config overrides win over both.
    if args.auto:
        auto = compute_auto_config(train_count)
    else:
        auto = {"iters": 100, "num_layers": 8, "learning_rate": 1e-5}

    knobs = resolve_train_knobs(
        BASE_MODEL,
        auto,
        iters_override=args.iters,
        num_layers_override=args.num_layers,
        learning_rate_override=args.learning_rate,
        sys_mem_gb=_system_mem_gb(),
    )
    iters = knobs["iters"]
    num_layers = knobs["num_layers"]
    learning_rate = knobs["learning_rate"]
    batch_size = knobs["batch_size"]
    max_seq_length = knobs["max_seq_length"]
    grad_checkpoint = knobs["grad_checkpoint"]

    config = {
        "base_model": BASE_MODEL,
        "data_dir": str(data_dir),
        "adapter_dir": str(adapter_dir),
        "iters": iters,
        "batch_size": batch_size,
        "num_layers": num_layers,
        "max_seq_length": max_seq_length,
        "grad_checkpoint": grad_checkpoint,
        "learning_rate": learning_rate,
        "train_pairs": train_count,
        "valid_pairs": valid_count,
        "auto_scaled": args.auto,
    }

    print("LoRA fine-tuning config:")
    for k, v in config.items():
        print(f"  {k}: {v}")

    if args.dry_run:
        print("\n--dry-run: exiting without training.")
        return RESULT_SKIP

    if not train_path.exists():
        print(f"\nSkip: {train_path} not found (no fresh feedback to train on).")
        return RESULT_SKIP

    if train_count < 3:
        print(f"\nSkip: only {train_count} training pairs (need at least 3; no-op).")
        return RESULT_SKIP

    adapter_dir.mkdir(parents=True, exist_ok=True)
    # Train into a STAGING dir, then atomically promote on success — a killed /
    # disk-full / sleep-mid-train run never leaves a half-written adapter in the
    # live served dir (which model_server would otherwise choke on) (b163).
    staging_dir = adapter_dir.parent / f"{adapter_dir.name}.staging"
    shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    # DPO mode
    dpo_path = ROOT_DIR / "data" / "dpo_train.jsonl"
    train_type_args: list[str] = []
    if getattr(args, "dpo", False) and dpo_path.exists():
        train_type_args = ["--train-type", "dpo"]
        data_dir = dpo_path.parent
        print("Using DPO training mode with", str(dpo_path))

    cmd = [
        "mlx_lm",
        "lora",
        "--model",
        BASE_MODEL,
        "--train",
        "--data",
        str(data_dir),
        "--adapter-path",
        str(staging_dir),
        "--iters",
        str(iters),
        "--batch-size",
        str(batch_size),
        "--num-layers",
        str(num_layers),
        "--learning-rate",
        str(learning_rate),
        *train_type_args,
    ]

    # b176 memory levers: cap sequence length (emails are short) and turn on
    # gradient checkpointing for larger bases so the GPU working set fits.
    if max_seq_length is not None:
        cmd.extend(["--max-seq-length", str(max_seq_length)])
    if grad_checkpoint:
        cmd.append("--grad-checkpoint")

    if valid_path.exists() and valid_count > 0:
        cmd.extend(["--val-batches", "1"])

    print(f"\nRunning: {' '.join(cmd)}\n")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)

    if result.returncode != 0:
        print(f"Training failed (exit {result.returncode}):")
        print(result.stderr)
        shutil.rmtree(staging_dir, ignore_errors=True)
        return RESULT_FAILURE

    # Validate + atomically promote the staged adapter into the live dir. If the
    # train produced no valid adapter (corrupt/empty), DON'T touch the live dir —
    # keep serving the previous good adapter rather than wedging on a bad one.
    if not _promote_adapter(staging_dir, adapter_dir):
        print("Training produced no valid adapter (staging adapters.safetensors missing/corrupt); not promoting.")
        shutil.rmtree(staging_dir, ignore_errors=True)
        return RESULT_FAILURE
    shutil.rmtree(staging_dir, ignore_errors=True)

    print(result.stdout)

    # Parse val loss from output
    val_loss = None
    for line in result.stdout.splitlines():
        m = re.search(r"Val loss[:\s]+([\d.]+)", line, re.IGNORECASE)
        if m:
            val_loss = float(m.group(1))

    # Mark feedback pairs as used — but only for global-adapter training.
    # Per-persona training re-uses the entire cohort each run (see the
    # `--persona` matching change in export_feedback_jsonl.py), so marking
    # those rows as used would prevent the global adapter from ever seeing
    # them again. The global adapter still wants the incremental
    # used_in_finetune behaviour, so we keep it for the no-persona path.
    persona = getattr(args, "persona", None)
    db_path = Path(args.db)
    pairs_used = 0
    if db_path.exists() and not persona:
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.execute("UPDATE feedback_pairs SET used_in_finetune = 1 WHERE used_in_finetune = 0")
            pairs_used = cursor.rowcount
            conn.commit()
        finally:
            conn.close()

    # Save metadata. Persona attribution lets downstream consumers (stats,
    # doctor, the routed generation in Phase 3) distinguish "global adapter
    # trained N pairs ago" from "internal persona adapter trained M pairs
    # ago" — without it both would just say "adapters.safetensors mtime".
    meta = {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "base_model": BASE_MODEL,
        "pairs_used": pairs_used or train_count,
        "iters": iters,
        "num_layers": num_layers,
        "batch_size": batch_size,
        "max_seq_length": max_seq_length,
        "grad_checkpoint": grad_checkpoint,
        "learning_rate": learning_rate,
        "final_val_loss": val_loss,
        "persona": persona,
    }
    meta_path = adapter_dir / "meta.json"
    # Atomic (b251): a torn meta.json is treated as "legacy adapter" by the
    # warm server, silently forfeiting the cross-base refusal (b174).
    from app.core.atomic_io import atomic_write_json

    atomic_write_json(meta_path, meta)

    print("\nTraining complete.")
    print(f"  Adapter saved to: {adapter_dir}")
    print(f"  Pairs used: {meta['pairs_used']}")
    print(f"  Val loss: {val_loss}")
    print(f"  Metadata: {meta_path}")

    return RESULT_SUCCESS


def main() -> None:
    args = parse_args()

    # Persona routing: when --persona is set and --adapter-dir wasn't
    # explicitly overridden, redirect the output to the persona-specific
    # sibling. Without this, persona training would overwrite the global
    # adapter at `adapters/latest/` — defeating the whole point of having
    # per-cohort adapters. Explicit --adapter-dir always wins for the
    # "I know what I'm doing" path (e.g. eval comparisons).
    if args.persona:
        from app.core.settings import get_adapter_path, get_persona_adapter_path

        default_global = str(get_adapter_path())
        if args.adapter_dir == default_global:
            args.adapter_dir = str(get_persona_adapter_path(args.persona))

    result = run_training(args)
    # Surface a genuine training failure with a nonzero exit so the nightly
    # pipeline's _run_step records finetune: false / [WARN] instead of [OK].
    # A SKIP (no/too-little fresh data, --dry-run) and a SUCCESS both exit 0.
    if result == RESULT_FAILURE:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
