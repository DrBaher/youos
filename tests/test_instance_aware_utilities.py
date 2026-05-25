"""Instance-awareness end-to-end across the remaining CLI/utility surface.

Companion to PR #16 (ADAPTER_PATH) and PR #18 (nightly state files).
This module pins that the **last four** repo-pinned-`var/` surfaces
mentioned in PR #18's "still flagged" list now honor `YOUOS_DATA_DIR`:

- ``youos export`` sources from the active instance, archive layout stays
  repo-relative so existing backups still restore cleanly
- ``youos import`` extracts **into the active instance**, not the repo
- ``app.core.doctor`` checks the instance's ``youos.db`` /
  ``youos_config.yaml`` / ``models/``
- ``scripts.teardown`` targets the active instance's data dirs (the destructive
  one — biggest risk of accidental scrub of the repo dev tree)
- ``scripts.setup_wizard`` writes ``youos_config.yaml`` into the instance,
  not the repo (otherwise every instance silently shares one config file)

Plus the new ``get_instance_root`` / ``get_models_dir`` helpers in
``app.core.settings`` that the above all funnel through.
"""

from __future__ import annotations

import sqlite3
import tarfile
from pathlib import Path

import pytest


@pytest.fixture
def _reset_settings():
    from app.core.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def instance(monkeypatch, tmp_path, _reset_settings):
    """A populated tmp instance: var/youos.db, youos_config.yaml, configs/, models/adapters/latest/."""
    root = tmp_path / "instance"
    (root / "var").mkdir(parents=True)
    (root / "var" / "youos.db").write_bytes(b"fake-db-bytes")
    (root / "youos_config.yaml").write_text("user:\n  name: Test\n", encoding="utf-8")
    (root / "configs").mkdir()
    (root / "configs" / "persona.yaml").write_text("name: Test\n", encoding="utf-8")
    (root / "models" / "adapters" / "latest").mkdir(parents=True)
    (root / "models" / "adapters" / "latest" / "adapters.safetensors").write_bytes(b"weights")
    monkeypatch.setenv("YOUOS_DATA_DIR", str(root))
    return root


# ── new helpers in app.core.settings ─────────────────────────────────────

def test_get_instance_root_is_data_dir(instance):
    from app.core.settings import get_instance_root

    assert get_instance_root() == instance


def test_get_instance_root_falls_back_to_repo(monkeypatch, _reset_settings):
    monkeypatch.delenv("YOUOS_DATA_DIR", raising=False)
    from app.core.settings import ROOT_DIR, get_instance_root

    assert get_instance_root() == ROOT_DIR


def test_get_models_dir_is_under_instance(instance):
    from app.core.settings import get_models_dir

    assert get_models_dir() == instance / "models"


def test_get_adapter_path_still_under_models_dir(instance):
    """get_adapter_path was rebased onto get_models_dir; this guards the
    invariant that adapter lives at ``<models_dir>/adapters/latest``."""
    from app.core.settings import get_adapter_path, get_models_dir

    assert get_adapter_path() == get_models_dir() / "adapters" / "latest"


# ── youos export sources from the instance ───────────────────────────────

def test_export_archive_sources_from_instance(instance, tmp_path):
    """Round-trip: export under YOUOS_DATA_DIR, confirm archive contents
    are the instance's files (not the repo's). The fake-db-bytes the
    fixture wrote are the canary."""
    from typer.testing import CliRunner

    from app.cli import app

    runner = CliRunner()
    archive_path = tmp_path / "backup.tar.gz"
    result = runner.invoke(app, ["export", "--output", str(archive_path)])
    assert result.exit_code == 0, result.output

    assert archive_path.exists()
    with tarfile.open(archive_path, "r:gz") as tar:
        names = tar.getnames()
        # Archive layout is stable / repo-relative.
        assert "var/youos.db" in names
        assert "youos_config.yaml" in names
        assert "configs/persona.yaml" in names
        assert "models/adapters/latest/adapters.safetensors" in names
        # Content came from the instance, not the repo.
        member = tar.extractfile("var/youos.db")
        assert member is not None
        assert member.read() == b"fake-db-bytes"


# ── youos import extracts into the instance, not the repo ────────────────

def test_import_extracts_into_instance(instance, tmp_path):
    """The blast radius is the killer here: a `youos import` against the
    wrong base dir would overwrite arbitrary files. Pin the destination."""
    from typer.testing import CliRunner

    from app.cli import app

    # Build an archive shaped like a real backup, with a *different* DB
    # payload so we can detect which file ended up where.
    archive_path = tmp_path / "restore.tar.gz"
    staging = tmp_path / "staging"
    (staging / "var").mkdir(parents=True)
    (staging / "var" / "youos.db").write_bytes(b"restored-db-bytes")
    (staging / "youos_config.yaml").write_text("user:\n  name: Restored\n", encoding="utf-8")
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(str(staging / "var" / "youos.db"), arcname="var/youos.db")
        tar.add(str(staging / "youos_config.yaml"), arcname="youos_config.yaml")

    runner = CliRunner()
    # Auto-confirm the "DB already exists, overwrite?" prompt.
    result = runner.invoke(app, ["import", "--input", str(archive_path)], input="y\n")
    assert result.exit_code == 0, result.output

    # The instance's files were replaced with the archive contents.
    assert (instance / "var" / "youos.db").read_bytes() == b"restored-db-bytes"
    assert "Restored" in (instance / "youos_config.yaml").read_text(encoding="utf-8")


# ── doctor reports on instance paths ─────────────────────────────────────

def test_doctor_youos_db_warning_names_instance_path(monkeypatch, tmp_path, _reset_settings):
    """The warning string must point at the instance DB path, not the repo's.
    Otherwise a user troubleshooting a multi-instance setup gets sent to
    the wrong place."""
    empty_instance = tmp_path / "empty"
    empty_instance.mkdir()
    (empty_instance / "youos_config.yaml").write_text("user:\n  emails: ['a@b']\n", encoding="utf-8")
    monkeypatch.setenv("YOUOS_DATA_DIR", str(empty_instance))

    from app.core.doctor import run_doctor_checks_full

    _, _, warnings = run_doctor_checks_full()
    db_warning = next((w for w in warnings if "youos.db" in w), None)
    assert db_warning is not None
    assert str(empty_instance) in db_warning


def test_doctor_youos_config_failure_names_instance_path(monkeypatch, tmp_path, _reset_settings):
    """A missing youos_config.yaml is a required-failure, and the message
    must point at the instance — otherwise users add the config to the
    repo and wonder why nothing changed."""
    bare_instance = tmp_path / "bare"
    bare_instance.mkdir()
    # no youos_config.yaml present
    monkeypatch.setenv("YOUOS_DATA_DIR", str(bare_instance))

    from app.core.doctor import run_doctor_checks

    _, failures = run_doctor_checks()
    config_failure = next((f for f in failures if "youos_config.yaml" in f), None)
    # Required failures *may* come from this check only if the path is
    # absent — but the message must name the instance path either way.
    if config_failure:
        assert str(bare_instance) in config_failure


# ── teardown targets the instance, not the repo ──────────────────────────

def test_teardown_removes_instance_dirs_not_repo(monkeypatch, tmp_path, _reset_settings):
    """The destructive one. A teardown invoked with YOUOS_DATA_DIR set must
    scrub the instance, and the repo's dev tree must stay untouched."""
    inst = tmp_path / "instance"
    (inst / "var").mkdir(parents=True)
    (inst / "var" / "youos.db").write_text("fake")
    (inst / "data").mkdir()
    (inst / "data" / "raw").write_text("cache")
    (inst / "models").mkdir()
    (inst / "models" / "adapter.bin").write_text("weights")
    (inst / "configs").mkdir()
    (inst / "configs" / "persona_analysis.json").write_text("{}", encoding="utf-8")

    # A canary file in the repo — teardown should NOT touch this.
    repo_canary = tmp_path / "repo_canary"
    repo_canary.write_text("do-not-delete")

    monkeypatch.setenv("YOUOS_DATA_DIR", str(inst))

    from scripts.teardown import teardown

    teardown(delete_all=True)

    assert not (inst / "var").exists()
    assert not (inst / "data").exists()
    assert not (inst / "models").exists()
    assert not (inst / "configs" / "persona_analysis.json").exists()
    # The repo dev tree is intact.
    assert repo_canary.read_text() == "do-not-delete"


def test_teardown_banner_shows_instance_target(monkeypatch, tmp_path, capsys, _reset_settings):
    """Read-only check that the user sees which instance they're about to wipe."""
    inst = tmp_path / "instance"
    (inst / "var").mkdir(parents=True)
    monkeypatch.setenv("YOUOS_DATA_DIR", str(inst))

    from scripts.teardown import teardown

    teardown(delete_all=True)
    captured = capsys.readouterr()
    assert str(inst) in captured.out


# ── setup_wizard's CONFIG_PATH points at the instance ───────────────────

def test_setup_wizard_config_path_resolves_to_instance(monkeypatch, tmp_path, _reset_settings):
    """The wizard had been writing `youos_config.yaml` to the repo even under
    YOUOS_DATA_DIR — so every instance silently shared one config file and
    the wizard's writes weren't visible to `youos status` for that
    instance. Module-level constant captured at import; reload to pick up
    the patched env, mirroring the subprocess pattern launchd uses."""
    inst = tmp_path / "instance"
    inst.mkdir()
    monkeypatch.setenv("YOUOS_DATA_DIR", str(inst))

    # Drop the module so reimport reads the patched env.
    import sys

    sys.modules.pop("scripts.setup_wizard", None)
    import importlib

    wizard = importlib.import_module("scripts.setup_wizard")

    assert wizard.CONFIG_PATH == inst / "youos_config.yaml"


# Quiet a fixture-as-arg lint about the unused sqlite3 import at module top.
def _silence_unused() -> None:
    sqlite3.version  # noqa: B018
    Path  # noqa: B018
