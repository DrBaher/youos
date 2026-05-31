"""Adapter promotion gate."""

from __future__ import annotations

from app.evaluation.promotion import (
    gate_after_eval,
    restore_adapter,
    should_promote,
    snapshot_adapter,
)


def test_should_promote_improve_hold_regress():
    # Relative (non-regression) gate: improve / hold-within-tolerance / regress.
    assert should_promote(0.60, 0.55)[0] is True          # improved
    assert should_promote(0.54, 0.55)[0] is True          # held within tolerance
    assert should_promote(0.50, 0.55)[0] is False         # regressed past tolerance
    # Missing values → keep (no basis to reject — cold start / eval unavailable).
    assert should_promote(None, 0.55)[0] is True
    assert should_promote(0.4, None)[0] is True


def test_should_promote_keeps_warm_sub_target_non_regressing():
    """b170 revision: promotion serves the BEST-AVAILABLE adapter. A WARM-path
    candidate that holds vs baseline is KEPT even when it is below the 0.5
    quality target (live composites sit ~0.30) — rolling it back would freeze
    the self-improvement loop. Only a genuine REGRESSION is rolled back."""
    ok, reason = should_promote(0.30, 0.28)  # sub-target, doesn't regress → kept
    assert ok is True
    assert "kept" in reason
    assert "below target" in reason
    # A real regression past tolerance is still rolled back.
    assert should_promote(0.20, 0.40)[0] is False
    # Cold start: no baseline → still promote.
    assert should_promote(0.30, None)[0] is True


def test_should_promote_min_floor_disabled_by_default():
    """The default safety floor is disabled (0.0) so the warm path is a pure
    non-regression gate; it must NOT default to the 0.5 quality target."""
    from app.evaluation.promotion import DEFAULT_MIN_FLOOR

    assert DEFAULT_MIN_FLOOR == 0.0
    # An explicit safety floor can still reject a genuinely broken adapter.
    ok, reason = should_promote(0.05, 0.04, min_floor=0.1)
    assert ok is False
    assert "safety floor" in reason


def test_should_promote_refuses_on_degenerate_eval():
    # A degenerate eval is a HARD refuse — overrides even the "missing values →
    # keep" default and an apparently-good composite.
    assert should_promote(None, None, eval_degenerate=True)[0] is False
    assert should_promote(0.9, 0.5, eval_degenerate=True)[0] is False
    ok, reason = should_promote(None, None, eval_degenerate=True)
    assert "degenerate" in reason


def _write_adapter(d, content):
    d.mkdir(parents=True, exist_ok=True)
    (d / "adapters.safetensors").write_text(content, encoding="utf-8")
    (d / "meta.json").write_text("{}", encoding="utf-8")


def test_snapshot_and_restore_round_trip(tmp_path):
    latest = tmp_path / "latest"
    prev = tmp_path / "previous"
    _write_adapter(latest, "GOOD")

    assert snapshot_adapter(latest, prev) is True
    assert (prev / "adapters.safetensors").read_text() == "GOOD"

    # A bad retrain overwrites latest...
    (latest / "adapters.safetensors").write_text("BAD", encoding="utf-8")
    # ...rollback restores the snapshot.
    assert restore_adapter(prev, latest) is True
    assert (latest / "adapters.safetensors").read_text() == "GOOD"


def test_snapshot_noop_when_no_adapter(tmp_path):
    assert snapshot_adapter(tmp_path / "missing", tmp_path / "prev") is False


def test_gate_keeps_when_improved(tmp_path):
    latest = tmp_path / "latest"
    prev = tmp_path / "previous"
    _write_adapter(latest, "NEW")
    snapshot_adapter(latest, prev)  # snapshot holds "NEW" as baseline copy
    # New adapter content stays.
    (latest / "adapters.safetensors").write_text("NEWER", encoding="utf-8")

    res = gate_after_eval(candidate_composite=0.6, baseline_composite=0.5,
                          latest_dir=latest, previous_dir=prev)
    assert res["action"] == "kept"
    assert (latest / "adapters.safetensors").read_text() == "NEWER"


def test_gate_rolls_back_when_regressed(tmp_path):
    latest = tmp_path / "latest"
    prev = tmp_path / "previous"
    _write_adapter(latest, "GOOD")
    snapshot_adapter(latest, prev)
    # Bad retrain landed in latest.
    (latest / "adapters.safetensors").write_text("BAD", encoding="utf-8")

    res = gate_after_eval(candidate_composite=0.30, baseline_composite=0.45,
                          latest_dir=latest, previous_dir=prev)
    assert res["action"] == "rolled_back"
    assert res["restored"] is True
    assert (latest / "adapters.safetensors").read_text() == "GOOD"


def test_gate_rolls_back_on_degenerate_eval_even_without_baseline(tmp_path):
    """A broken (all-empty) eval must not let a new adapter through, even on a
    first run with no baseline to compare against."""
    latest = tmp_path / "latest"
    prev = tmp_path / "previous"
    _write_adapter(latest, "GOOD")
    snapshot_adapter(latest, prev)
    (latest / "adapters.safetensors").write_text("BAD", encoding="utf-8")

    res = gate_after_eval(candidate_composite=None, baseline_composite=None,
                          latest_dir=latest, previous_dir=prev, eval_degenerate=True)
    assert res["action"] == "rolled_back"
    assert (latest / "adapters.safetensors").read_text() == "GOOD"


# --- b143: promotion-gate baseline + degenerate-first-run discard ------------


def test_composite_to_persist_uses_baseline_after_rollback():
    """After a rollback the LIVE adapter is the previous snapshot (baseline), so
    persisting the rejected candidate would lower the bar and defeat the gate."""
    from app.evaluation.promotion import composite_to_persist

    assert composite_to_persist("rolled_back", 0.30, 0.80) == 0.80  # bad candidate not persisted
    assert composite_to_persist("kept", 0.85, 0.80) == 0.85
    assert composite_to_persist("rollback_failed", 0.30, 0.80) == 0.30  # bad adapter still live
    assert composite_to_persist("rolled_back", 0.30, None) is None


def test_discard_adapter_clears_files(tmp_path):
    """First-ever degenerate finetune has no snapshot to roll back to — discard
    the untrustworthy adapter rather than serve it."""
    from app.evaluation.promotion import discard_adapter

    d = tmp_path / "latest"
    d.mkdir()
    (d / "adapter.safetensors").write_text("x")
    (d / "adapter_config.json").write_text("{}")
    assert discard_adapter(d) is True
    assert list(d.iterdir()) == []
    assert discard_adapter(d) is False  # nothing left to discard
