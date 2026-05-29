"""Adapter promotion gate."""

from __future__ import annotations

from app.evaluation.promotion import (
    gate_after_eval,
    restore_adapter,
    should_promote,
    snapshot_adapter,
)


def test_should_promote_improve_hold_regress():
    assert should_promote(0.50, 0.45)[0] is True          # improved
    assert should_promote(0.44, 0.45)[0] is True          # held within tolerance
    assert should_promote(0.40, 0.45)[0] is False         # regressed past tolerance
    # Missing values → keep (no basis to reject).
    assert should_promote(None, 0.45)[0] is True
    assert should_promote(0.4, None)[0] is True


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
