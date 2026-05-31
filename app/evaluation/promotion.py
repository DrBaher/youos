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
# Aspirational quality TARGET (mirrors nightly_pipeline.GOLDEN_PASS_BAR). Used
# only for HONEST MESSAGING in the kept-reason — NOT as a promotion threshold.
# A kept sub-target adapter says "below target X but kept (doesn't regress)" so
# the gate log reconciles with the step's "[WARN] below target" report.
GOLDEN_TARGET = 0.5
# Optional ABSOLUTE safety floor for a WARM-path candidate (b170 revision).
#
# Promotion is fundamentally a DON'T-REGRESS decision: serve the best-available
# adapter so new feedback keeps improving the served model. It is deliberately
# NOT tied to the nightly step's aspirational quality TARGET (GOLDEN_PASS_BAR =
# 0.5) — that target reports "are we there yet"; promotion answers "is this run
# at least as good as the last one". An earlier b170 cut set this floor to 0.5
# and rolled back every sub-target retrain. But live composites sit around 0.30,
# so EVERY nightly retrain was rolled back — freezing the self-improvement loop
# (new feedback never reached the served adapter). That was operationally wrong.
#
# So the floor defaults to 0.0 = DISABLED: the warm path is the pure relative
# (non-regression) gate. The ``eval_degenerate`` check already catches a
# broken/empty adapter, so a genuine "this adapter is broken" floor adds nothing
# at current quality. The param is kept only as a future hook (e.g. once quality
# clears the target you might set a real, far-below-current floor); it must NOT
# be set to the 0.5 target.
DEFAULT_MIN_FLOOR = 0.0


def should_promote(
    candidate: float | None,
    baseline: float | None,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    min_floor: float = DEFAULT_MIN_FLOOR,
    eval_degenerate: bool = False,
) -> tuple[bool, str]:
    """Decide keep-vs-rollback for a freshly-trained adapter.

    Rule (b170 revision) — serve the BEST-AVAILABLE adapter, never freeze:

    * ``eval_degenerate`` is a HARD refuse that overrides everything: when the
      golden eval is untrustworthy (the model returned mostly empty drafts),
      its composite is meaningless, so we never keep an adapter "validated" by
      it — even on a first run with no baseline to compare against.
    * Missing values (no baseline yet = cold start / first finetune, or the
      eval was unavailable so there's no candidate) ⇒ promote. There is no
      live adapter to fall back on, so we accept whatever the first run
      produces rather than leave drafting with no adapter. Preserves cold start.
    * WARM path (both values present): promote iff the candidate does NOT
      regress beyond ``tolerance`` (candidate >= baseline − tolerance). This is
      the relative non-regression gate: a sub-target (e.g. ~0.30) adapter that
      holds vs the prior run is PROMOTED, so new feedback keeps reaching the
      served model. ``min_floor`` defaults to 0.0 (disabled); it is an OPTIONAL
      genuine "this adapter is broken" floor, NOT the 0.5 quality target.

    Note (semantics): the nightly step's pass/fail vs GOLDEN_PASS_BAR (0.5) is a
    quality-TARGET report ("are we there yet"); this gate is a don't-regress
    decision. They answer different questions and legitimately differ — a sub-0.5
    composite is reported ``[WARN] below target`` yet still promoted if it does
    not regress. The nightly log makes that explicit so an operator isn't
    confused by "below target" alongside a kept adapter.
    """
    if eval_degenerate:
        return False, "golden eval degenerate (mostly empty drafts) — refusing to promote on an untrustworthy eval"
    if candidate is None or baseline is None:
        return True, "no baseline/candidate composite — keeping new adapter (cold start)"
    if candidate < baseline - tolerance:
        return False, f"composite regressed {baseline:.3f} → {candidate:.3f} (drop > {tolerance:.3f})"
    if min_floor > 0 and candidate < min_floor:
        return False, (
            f"composite {candidate:.3f} holds vs baseline {baseline:.3f} but is "
            f"below safety floor {min_floor:.2f} — rolling back"
        )
    base = f"composite {candidate:.3f} ≥ baseline {baseline:.3f} − {tolerance:.3f} (doesn't regress, kept)"
    if candidate < GOLDEN_TARGET:
        return True, base + f" — below target {GOLDEN_TARGET:.2f} but kept (doesn't regress vs prior)"
    return True, base


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
    min_floor: float = DEFAULT_MIN_FLOOR,
    eval_degenerate: bool = False,
) -> dict[str, Any]:
    """Decide keep-vs-rollback after the post-finetune eval. ``previous_dir``
    must already hold the pre-finetune snapshot. Returns an action dict for the
    nightly log: ``action`` ∈ {kept, rolled_back, rollback_failed}. A degenerate
    eval forces a rollback (the new adapter can't be trusted as validated). The
    warm path keeps the candidate iff it does NOT regress beyond ``tolerance``
    vs baseline (b170 revision) — a sub-target but non-regressing adapter is
    KEPT so the self-improvement loop never freezes; cold start (no baseline) is
    exempt. ``min_floor`` defaults to 0.0 (disabled)."""
    ok, reason = should_promote(
        candidate_composite, baseline_composite,
        tolerance=tolerance, min_floor=min_floor, eval_degenerate=eval_degenerate,
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
