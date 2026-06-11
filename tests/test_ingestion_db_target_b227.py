"""Gmail ingestion writes to the ACTIVE INSTANCE database (b227).

Forensic finding (baheros, 2026-06-11): `_default_sqlite_path()` read only the
raw YOUOS_DATABASE_URL env var with a repo-relative `sqlite:///var/youos.db`
fallback, ignoring YOUOS_DATA_DIR. The nightly's ingestion subprocess therefore
wrote every ingested thread into the repo's dev DB instead of the instance DB —
prod reply_pairs sat frozen at the original bulk import while "finetune: 0 new
pairs" repeated nightly. Since 2026-06-02 it hard-failed on the dev DB's legacy
schema ("table reply_pairs has no column named language") because
`_ensure_sqlite_schema` only ran CREATE-IF-NOT-EXISTS, never the ALTER
migrations.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.ingestion.gmail_threads import _default_sqlite_path, _ensure_sqlite_schema


@pytest.fixture
def _reset_settings():
    from app.core.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_default_sqlite_path_honors_data_dir(monkeypatch, tmp_path, _reset_settings):
    """With YOUOS_DATA_DIR set (the launchd/prod shape), ingestion must target
    the instance DB — not the repo-relative var/youos.db."""
    monkeypatch.delenv("YOUOS_DATABASE_URL", raising=False)
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path / "instance"))
    (tmp_path / "instance" / "var").mkdir(parents=True)

    assert _default_sqlite_path() == (tmp_path / "instance" / "var" / "youos.db").resolve()


def test_default_sqlite_path_explicit_url_wins(monkeypatch, tmp_path, _reset_settings):
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path / "instance"))
    monkeypatch.setenv("YOUOS_DATABASE_URL", f"sqlite:///{tmp_path}/elsewhere.db")
    (tmp_path / "instance").mkdir()

    assert _default_sqlite_path() == tmp_path / "elsewhere.db"


def test_default_sqlite_path_rejects_non_sqlite(monkeypatch, _reset_settings):
    monkeypatch.delenv("YOUOS_DATA_DIR", raising=False)
    monkeypatch.setenv("YOUOS_DATABASE_URL", "postgres://nope")

    with pytest.raises(ValueError, match="sqlite"):
        _default_sqlite_path()


def test_ensure_schema_upgrades_legacy_reply_pairs(tmp_path):
    """A pre-existing DB whose reply_pairs predates the `language` column must
    be ALTERed up, not left to crash the reply-pair INSERT."""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE reply_pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            document_id INTEGER,
            thread_id TEXT,
            inbound_text TEXT NOT NULL,
            reply_text TEXT NOT NULL,
            inbound_author TEXT,
            reply_author TEXT,
            paired_at TEXT,
            metadata_json TEXT,
            UNIQUE (source_type, source_id)
        )
        """
    )
    conn.commit()
    conn.close()

    _ensure_sqlite_schema(db_path)

    conn = sqlite3.connect(db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(reply_pairs)").fetchall()}
    finally:
        conn.close()
    assert "language" in cols and "quality_score" in cols
