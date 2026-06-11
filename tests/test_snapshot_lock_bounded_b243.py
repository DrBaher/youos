"""b243: snapshot lock + backup copies are BOUNDED, and whole-DB writers
share the lock.

The hang chain this kills: a restore stalled behind a busy writer held the
flock forever (Python's sqlite3 backup retries SQLITE_BUSY indefinitely);
the nightly's create_snapshot — its FIRST step — then blocked on the flock,
silently hanging the entire pipeline with no error and no timeout.
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import time

import pytest

from app.core import data_safety as ds


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()


def test_snapshot_lock_acquisition_times_out(tmp_path, monkeypatch):
    db = tmp_path / "youos.db"
    _make_db(db)
    monkeypatch.setattr(ds, "_LOCK_ACQUIRE_DEADLINE_SECONDS", 0.6)
    # Hold the flock on a separate fd (flock conflicts across open file
    # descriptions, even in the same process).
    holder = os.open(tmp_path / ".snapshot.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX)
        t0 = time.monotonic()
        with pytest.raises(TimeoutError, match="snapshot.lock"):
            ds.create_snapshot(db, tier="manual")
        assert time.monotonic() - t0 < 10  # bounded, not forever
    finally:
        os.close(holder)


def test_backup_copy_times_out_behind_busy_writer(tmp_path, monkeypatch):
    db = tmp_path / "youos.db"
    _make_db(db)
    snap = ds.create_snapshot(db, tier="manual")
    monkeypatch.setattr(ds, "_BACKUP_DEADLINE_SECONDS", 1.0)
    writer = sqlite3.connect(db)
    try:
        writer.execute("BEGIN IMMEDIATE")  # same lock class as a wedged writer
        t0 = time.monotonic()
        with pytest.raises(TimeoutError, match="busy"):
            ds.restore_snapshot(db, snap)
        assert time.monotonic() - t0 < 30  # previously: stalled indefinitely
    finally:
        writer.close()


def test_prune_reclaims_pre_restore_backups(tmp_path, monkeypatch):
    db = tmp_path / "youos.db"
    _make_db(db)
    monkeypatch.setattr(ds, "_PRE_RESTORE_KEEP", 2)
    for i in range(5):
        p = tmp_path / f"youos.pre-restore-2026061{i}-000000-000000-aa.db"
        p.write_bytes(b"x")
        os.utime(p, (1000 + i, 1000 + i))
    removed = ds.prune_snapshots(db)
    assert removed.get("pre_restore") == 3
    survivors = sorted(q.name for q in tmp_path.glob("youos.pre-restore-*.db"))
    assert len(survivors) == 2
    assert survivors == [
        "youos.pre-restore-20260613-000000-000000-aa.db",
        "youos.pre-restore-20260614-000000-000000-aa.db",
    ]  # newest kept


def test_bootstrap_database_waits_on_snapshot_lock(tmp_path, monkeypatch):
    """bootstrap (DDL + FTS rebuild) must queue behind a snapshot op, not
    interleave with it — verified via the shared lock timing out."""
    from pathlib import Path

    import app.db.bootstrap as bootstrap

    repo_root = Path(bootstrap.__file__).resolve().parents[2]

    class _S:
        database_url = f"sqlite:///{tmp_path}/youos.db"
        configs_dir = repo_root / "configs"  # so docs/schema.sql resolves

    monkeypatch.setattr(bootstrap, "get_settings", lambda: _S())
    monkeypatch.setattr(ds, "_LOCK_ACQUIRE_DEADLINE_SECONDS", 0.6)
    holder = os.open(tmp_path / ".snapshot.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX)
        with pytest.raises(TimeoutError):
            bootstrap.bootstrap_database()
    finally:
        os.close(holder)
    # and with the lock free, bootstrap succeeds
    assert bootstrap.bootstrap_database().exists()
