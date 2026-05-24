"""Autoresearch must operate on the active instance (YOUOS_DATA_DIR), not the repo.

Regression test for the bug where run_autoresearch.py hardcoded ROOT_DIR/var/
youos.db and ROOT_DIR/configs, so it always optimized the repo's default config
against the repo DB and never the instance it was meant to tune.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import scripts.run_autoresearch as ar
from app.core.settings import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    # get_settings is lru_cached; clear around each test so env changes take and
    # don't leak instance paths into other tests.
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_resolve_paths_uses_instance_from_data_dir(tmp_path, monkeypatch):
    inst = tmp_path / "myinst"
    (inst / "var").mkdir(parents=True)
    (inst / "configs").mkdir()
    monkeypatch.setenv("YOUOS_DATA_DIR", str(inst))
    monkeypatch.delenv("YOUOS_DATABASE_URL", raising=False)
    monkeypatch.delenv("YOUOS_CONFIGS_DIR", raising=False)
    get_settings.cache_clear()

    db_url, configs = ar.resolve_paths(None, None)

    assert db_url.endswith("var/youos.db")
    assert str(inst.resolve()) in db_url
    assert Path(configs).resolve() == (inst / "configs").resolve()


def test_resolve_paths_explicit_overrides_win(tmp_path, monkeypatch):
    inst = tmp_path / "myinst"
    (inst / "var").mkdir(parents=True)
    monkeypatch.setenv("YOUOS_DATA_DIR", str(inst))
    get_settings.cache_clear()

    db = tmp_path / "explicit.db"
    cfg = tmp_path / "explicit_configs"
    db_url, configs = ar.resolve_paths(db, cfg)

    assert db_url == f"sqlite:///{db}"
    assert configs == cfg


def test_resolve_paths_falls_back_to_repo_without_data_dir(monkeypatch):
    monkeypatch.delenv("YOUOS_DATA_DIR", raising=False)
    monkeypatch.delenv("YOUOS_DATABASE_URL", raising=False)
    get_settings.cache_clear()

    db_url, configs = ar.resolve_paths(None, None)

    # Repo default DB; not pointed at any instance directory.
    assert db_url.endswith("var/youos.db")
    assert "instances" not in db_url
