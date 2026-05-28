"""Bootstrap + startup-safety regressions (review-driven hardening).

These pin two assertions that the OpenClaw code review wanted as standing
guarantees: a fresh instance auto-bootstraps its DB cleanly (so first-run
isn't a manual schema dance), and unsafe paths fail-fast with a clear
RuntimeError (so a wrong YOUOS_DATA_DIR can't quietly start a half-broken
server pointed at, say, the Trash).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.core.data_safety import validate_instance_paths
from app.core.settings import Settings, get_settings
from app.db.bootstrap import bootstrap_database

REQUIRED_TABLES = {"documents", "chunks", "reply_pairs", "feedback_pairs"}


def test_missing_db_auto_bootstraps_cleanly(tmp_path, monkeypatch):
    """`bootstrap_database()` on a fresh instance creates a usable DB with the
    required tables. No pre-existing file, no manual schema step."""
    # Stand up a fresh instance root under tmp_path with the schema in place
    # (bootstrap_database resolves schema.sql relative to configs_dir.parent).
    (tmp_path / "var").mkdir()
    (tmp_path / "configs").mkdir()
    docs = tmp_path / "docs"
    docs.mkdir()
    repo_schema = Path(__file__).resolve().parents[1] / "docs" / "schema.sql"
    (docs / "schema.sql").write_text(repo_schema.read_text(encoding="utf-8"))

    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("YOUOS_DATABASE_URL", f"sqlite:///{tmp_path}/var/youos.db")
    get_settings.cache_clear()
    try:
        db_path = bootstrap_database()
        assert db_path.exists(), "bootstrap did not create the DB file"
        conn = sqlite3.connect(db_path)
        try:
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
        missing = REQUIRED_TABLES - names
        assert not missing, f"bootstrap left required tables missing: {missing}"
    finally:
        get_settings.cache_clear()


def test_unsafe_db_path_fails_fast_with_clear_error(tmp_path):
    """Pointing the database at a Trash-like path must raise a clear
    RuntimeError, not silently start a server against an unsafe location."""
    unsafe = tmp_path / ".Trash" / "youos.db"
    s = Settings(database_url=f"sqlite:///{unsafe}")
    with pytest.raises(RuntimeError, match="(?i)unsafe"):
        validate_instance_paths(s)
