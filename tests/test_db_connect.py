"""The shared SQLite connection helper must set busy_timeout + WAL.

Without a busy_timeout, the generation path (which opens several connections per
draft) and the nightly pipeline (which runs while the web server is live) hit
'database is locked' instead of briefly waiting for the lock.
"""

from __future__ import annotations

from app.db.bootstrap import SQLITE_BUSY_TIMEOUT_MS, connect


def test_connect_sets_busy_timeout(tmp_path):
    conn = connect(tmp_path / "t.db")
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == SQLITE_BUSY_TIMEOUT_MS
    finally:
        conn.close()


def test_connect_enables_wal(tmp_path):
    conn = connect(tmp_path / "t.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_connect_busy_timeout_is_generous():
    # Must comfortably exceed Python's 5s default so a live server doesn't trip it.
    assert SQLITE_BUSY_TIMEOUT_MS >= 15000
