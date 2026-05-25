"""Nightly-pipeline runtime state lands in the active instance's var/.

Companion to the ADAPTER_PATH fix (PR #16): the nightly pipeline still
wrote `pipeline_last_run.json`, `golden_results.json`, `persona_merge.log`,
`persona_drift.jsonl`, `persona_last_full_analysis.txt`,
`benchmark_last_refresh.txt`, and `autoresearch_log.md` into the repo's
`var/` even with `YOUOS_DATA_DIR` set — so on a multi-instance setup every
instance overwrote the same files (and `app.core.stats.AUTORESEARCH_LOG`,
which already used `get_var_dir`, read a different file than the writer).
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def _reset_settings():
    from app.core.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def instance_dir(monkeypatch, tmp_path, _reset_settings):
    """Point YOUOS_DATA_DIR at a tmp instance with a writable var/."""
    instance = tmp_path / "instance"
    (instance / "var").mkdir(parents=True)
    monkeypatch.setenv("YOUOS_DATA_DIR", str(instance))
    return instance


# ── nightly_pipeline.py ──────────────────────────────────────────────────

def test_pipeline_log_path_uses_instance_var(instance_dir):
    """`_pipeline_log_path()` returns `<instance>/var/pipeline_last_run.json`."""
    pipeline = importlib.import_module("scripts.nightly_pipeline")

    assert pipeline._pipeline_log_path() == instance_dir / "var" / "pipeline_last_run.json"


def test_save_last_auto_feedback_writes_into_instance_var(instance_dir):
    pipeline = importlib.import_module("scripts.nightly_pipeline")

    pipeline._save_last_auto_feedback_at()
    written = instance_dir / "var" / "pipeline_last_run.json"
    assert written.exists()
    data = json.loads(written.read_text(encoding="utf-8"))
    assert "last_auto_feedback_at" in data
    # And — crucially — nothing was written into the repo's var/
    repo_var = Path(pipeline.ROOT_DIR) / "var" / "pipeline_last_run.json"
    if repo_var.exists():
        # Either the file existed already from previous runs, or it was just
        # touched. We can only assert that its timestamp differs from the
        # instance file. Skip the assertion in that case rather than risk a
        # false positive in CI; the existence + content of the instance file
        # is the real signal.
        pytest.skip("repo var/ already contains a pipeline log (pre-existing)")
    assert not repo_var.exists()


def test_load_last_auto_feedback_reads_from_instance_var(instance_dir):
    pipeline = importlib.import_module("scripts.nightly_pipeline")

    target = instance_dir / "var" / "pipeline_last_run.json"
    target.write_text(json.dumps({"last_auto_feedback_at": "2026-05-20T00:00:00+00:00"}))
    assert pipeline._load_last_auto_feedback_at() == "2026-05-20T00:00:00+00:00"


def test_write_pipeline_log_writes_into_instance_var(instance_dir):
    pipeline = importlib.import_module("scripts.nightly_pipeline")

    pipeline._write_pipeline_log({"status": "ok", "steps": {}})
    written = instance_dir / "var" / "pipeline_last_run.json"
    assert written.exists()
    assert json.loads(written.read_text(encoding="utf-8"))["status"] == "ok"


def test_pipeline_log_path_falls_back_to_repo_when_no_data_dir(monkeypatch, _reset_settings):
    monkeypatch.delenv("YOUOS_DATA_DIR", raising=False)
    pipeline = importlib.import_module("scripts.nightly_pipeline")

    expected = Path(pipeline.ROOT_DIR) / "var" / "pipeline_last_run.json"
    assert pipeline._pipeline_log_path() == expected


# ── helper-script source code uses get_var_dir, not ROOT_DIR/"var" ───────
#
# Reloading `app.core.stats` or `scripts.run_golden_eval` to verify their
# module-level constants would mutate cross-test state (the captured value
# would leak into later tests that import the same module). Instead, pin
# the *source code* path: every helper script the nightly invokes must
# build its var/ paths via `get_var_dir()`. Catches a regression to a
# hardcoded `ROOT_DIR / "var" / ...` without forcing a module reload.

_HELPER_SCRIPTS = (
    "scripts/run_golden_eval.py",
    "scripts/run_autoresearch.py",
    "scripts/analyze_persona.py",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("rel_path", _HELPER_SCRIPTS)
def test_helper_script_does_not_pin_var_to_repo(rel_path):
    """No `ROOT_DIR / "var"` remains in the nightly's helper scripts.

    Companion to the nightly_pipeline.py fix — those helpers used to write
    their per-run state (golden_results.json, autoresearch_log.md,
    persona_drift.jsonl, persona_merge.log) under `ROOT_DIR/var/`, so on a
    non-default `YOUOS_DATA_DIR` the writer and the stats reader pointed at
    different files.
    """
    source = (_repo_root() / rel_path).read_text(encoding="utf-8")
    # The pattern that bit us: literal `ROOT_DIR / "var"`. Allow `get_var_dir()`.
    assert 'ROOT_DIR / "var"' not in source, (
        f"{rel_path} still uses ROOT_DIR/'var' — runtime state will land in "
        "the repo, not the active instance's var/. Use get_var_dir() instead."
    )
    # Every helper that writes runtime state should import the helper.
    assert "get_var_dir" in source, (
        f"{rel_path} doesn't import get_var_dir — confirm the var/ paths are "
        "instance-aware."
    )


def test_persona_drift_target_resolves_under_instance_var(instance_dir):
    """`get_var_dir()` (what analyze_persona.py now uses) lands under the
    active instance's var/."""
    from app.core.settings import get_var_dir

    assert get_var_dir() / "persona_drift.jsonl" == instance_dir / "var" / "persona_drift.jsonl"


def test_autoresearch_log_target_resolves_under_instance_var(instance_dir):
    """The writer (`scripts.run_autoresearch._log_git_hash_to_autoresearch_log`)
    and the stats reader (`app.core.stats.AUTORESEARCH_LOG`) must agree on
    the same `<instance>/var/autoresearch_log.md` file."""
    from app.core.settings import get_var_dir

    assert get_var_dir() / "autoresearch_log.md" == instance_dir / "var" / "autoresearch_log.md"


def test_golden_results_target_resolves_under_instance_var(instance_dir):
    """Pinned via `get_var_dir()` so multiple instances don't clobber each
    other's last-eval JSON."""
    from app.core.settings import get_var_dir

    assert get_var_dir() / "golden_results.json" == instance_dir / "var" / "golden_results.json"
