"""Adapter promotion gate — don't let a bad nightly retrain silently win.

`finetune_lora` writes the new adapter straight into `models/adapters/latest/`
and the warm model server reloads it — so a bad retrain (low-quality pairs, an
over-fit cohort) silently degraded every subsequent draft with no rollback.

This gates promotion on the golden-eval composite: snapshot the current adapter
before fine-tuning, and after the post-finetune eval, keep the new one only if
it holds or improves within tolerance — otherwise roll back to the snapshot.

The functions are pure/filesystem-only and unit-tested; the nightly wires them
around the existing finetune + golden-eval steps.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

DEFAULT_TOLERANCE = 0.02


def should_promote(
    candidate: float | None,
    baseline: float | None,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    eval_degenerate: bool = False,
) -> tuple[bool, str]:
    """Promote when the candidate composite holds or improves within
    ``tolerance`` of the baseline. Missing values ⇒ promote (no basis to
    reject — first run, or eval unavailable).

    ``eval_degenerate`` is a HARD refuse that overrides everything: when the
    golden eval is untrustworthy (the model returned mostly empty drafts), its
    composite is meaningless, so we must NOT keep a new adapter "validated" by
    it — even on a first run where there's no baseline to compare against."""
    if eval_degenerate:
        return False, "golden eval degenerate (mostly empty drafts) — refusing to promote on an untrustworthy eval"
    if candidate is None or baseline is None:
        return True, "no baseline/candidate composite — keeping new adapter"
    if candidate >= baseline - tolerance:
        return True, f"composite {candidate:.3f} ≥ baseline {baseline:.3f} − {tolerance:.3f} (kept)"
    return False, f"composite regressed {baseline:.3f} → {candidate:.3f} (drop > {tolerance:.3f})"


def _adapter_files(d: Path) -> list[Path]:
    real = d.resolve() if d.exists() else d
    return [p for p in real.iterdir() if p.is_file()] if real.is_dir() else []


def snapshot_adapter(latest_dir: Path | str, previous_dir: Path | str) -> bool:
    """Copy the current adapter (``latest``) to ``previous`` as a rollback
    point. Resolves symlinks (dev installs symlink ``latest``). Returns False if
    there's nothing to snapshot."""
    latest = Path(latest_dir)
    files = _adapter_files(latest)
    if not files:
        return False
    prev = Path(previous_dir)
    if prev.exists():
        shutil.rmtree(prev)
    prev.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy2(f, prev / f.name)
    return True


def restore_adapter(previous_dir: Path | str, latest_dir: Path | str) -> bool:
    """Restore the snapshot into ``latest`` (rollback). Uses ``shutil.copy`` so
    the restored files get a fresh mtime — the warm model server reloads on
    adapter mtime change, so the good adapter actually goes back live. Returns
    False if there's no snapshot."""
    prev = Path(previous_dir)
    prev_files = _adapter_files(prev)
    if not prev_files:
        return False
    latest = Path(latest_dir)
    real = latest.resolve() if latest.exists() else latest
    real.mkdir(parents=True, exist_ok=True)
    for f in list(real.iterdir()):
        if f.is_file():
            f.unlink()
    for f in prev_files:
        shutil.copy(f, real / f.name)  # copy (not copy2) → fresh mtime → triggers reload
    return True


def gate_after_eval(
    *,
    candidate_composite: float | None,
    baseline_composite: float | None,
    latest_dir: Path | str,
    previous_dir: Path | str,
    tolerance: float = DEFAULT_TOLERANCE,
    eval_degenerate: bool = False,
) -> dict[str, Any]:
    """Decide keep-vs-rollback after the post-finetune eval. ``previous_dir``
    must already hold the pre-finetune snapshot. Returns an action dict for the
    nightly log: ``action`` ∈ {kept, rolled_back, rollback_failed}. A degenerate
    eval forces a rollback (the new adapter can't be trusted as validated)."""
    ok, reason = should_promote(
        candidate_composite, baseline_composite,
        tolerance=tolerance, eval_degenerate=eval_degenerate,
    )
    if ok:
        return {"action": "kept", "reason": reason,
                "candidate": candidate_composite, "baseline": baseline_composite}
    restored = restore_adapter(previous_dir, latest_dir)
    return {
        "action": "rolled_back" if restored else "rollback_failed",
        "reason": reason,
        "restored": restored,
        "candidate": candidate_composite,
        "baseline": baseline_composite,
    }


def composite_to_persist(
    gate_action: str, candidate: float | None, baseline: float | None
) -> float | None:
    """The golden composite to persist as the NEXT run's baseline.

    After a successful ``rolled_back``, the live adapter is the pre-finetune
    snapshot (whose score is ``baseline``), NOT the rejected candidate — so we
    must persist ``baseline``. Persisting the (lower) candidate would lower the
    bar so the next regressed retrain could 'improve' past it, silently defeating
    the gate. ``rollback_failed`` keeps the candidate (the bad adapter IS still
    live, so its score is the honest baseline); ``kept`` keeps the candidate."""
    if gate_action == "rolled_back":
        return baseline
    return candidate


def discard_adapter(latest_dir: Path | str) -> bool:
    """Remove the adapter files from ``latest`` (no rollback target available).

    Used when a FIRST-EVER finetune produces a degenerate eval: there's no prior
    snapshot to restore to, so serve nothing (fall back to the base model)
    rather than keep an untrustworthy adapter the gate can't validate. Returns
    True if anything was removed."""
    latest = Path(latest_dir)
    files = _adapter_files(latest)
    if not files:
        return False
    real = latest.resolve() if latest.exists() else latest
    for f in list(real.iterdir()):
        if f.is_file():
            f.unlink()
    return True
