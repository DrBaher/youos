from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core.data_safety import (
    create_snapshot,
    list_snapshots,
    restore_snapshot,
    run_startup_safety_checks,
    validate_instance_paths,
)
from app.core.settings import Settings


def _mk_db(path: Path, *, with_memory: bool = True, with_draft_history: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS reply_pairs(id INTEGER PRIMARY KEY)")
        if with_draft_history:
            conn.execute("CREATE TABLE IF NOT EXISTS draft_history(id INTEGER PRIMARY KEY)")
        if with_memory:
            conn.execute("CREATE TABLE IF NOT EXISTS memory(id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()


def test_validate_instance_paths_rejects_trash(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "instance"
    (data_dir / "var").mkdir(parents=True)
    (data_dir / "configs").mkdir(parents=True)
    monkeypatch.setenv("YOUOS_DATABASE_URL", "sqlite:///Users/test/.Trash/youos.db")
    settings = Settings(
        data_dir=data_dir,
        configs_dir=data_dir / "configs",
    )
    with pytest.raises(RuntimeError):
        validate_instance_paths(settings)


def test_validate_instance_paths_rejects_mismatched_db(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "instance2"
    (data_dir / "var").mkdir(parents=True)
    (data_dir / "configs").mkdir(parents=True)
    other = tmp_path / "other" / "youos.db"
    other.parent.mkdir(parents=True)
    monkeypatch.setenv("YOUOS_DATABASE_URL", f"sqlite:///{other}")
    settings = Settings(
        data_dir=data_dir,
        configs_dir=data_dir / "configs",
    )
    with pytest.raises(RuntimeError):
        validate_instance_paths(settings)


def test_run_startup_safety_checks_warns_on_missing_tables(tmp_path: Path):
    data_dir = tmp_path / "inst"
    db = data_dir / "var" / "youos.db"
    _mk_db(db, with_memory=False, with_draft_history=False)

    settings = Settings(
        data_dir=data_dir,
        database_url=f"sqlite:///{db}",
        configs_dir=data_dir / "configs",
    )
    (data_dir / "configs").mkdir(parents=True, exist_ok=True)

    report = run_startup_safety_checks(settings)
    assert any("Required table missing: memory" in w for w in report.warnings)
    assert any("Required table missing: draft_history" in w for w in report.warnings)


def test_snapshot_create_list_restore(tmp_path: Path):
    db = tmp_path / "var" / "youos.db"
    _mk_db(db)

    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO reply_pairs DEFAULT VALUES")
    conn.commit()
    conn.close()

    snap = create_snapshot(db, tier="manual")
    assert snap.exists()

    snaps = list_snapshots(db)
    assert any(p == snap for p in snaps)

    # mutate db then restore
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM reply_pairs")
    conn.commit()
    conn.close()

    backup_path = restore_snapshot(db, snap)
    assert backup_path.exists()

    conn = sqlite3.connect(db)
    count = conn.execute("SELECT COUNT(*) FROM reply_pairs").fetchone()[0]
    conn.close()
    assert count == 1


def test_create_snapshot_rejects_traversal_tier(tmp_path: Path):
    db = tmp_path / "var" / "youos.db"
    _mk_db(db)
    with pytest.raises(ValueError):
        create_snapshot(db, tier="../../../tmp/evil")


def test_create_snapshot_rejects_path_separator_tier(tmp_path: Path):
    db = tmp_path / "var" / "youos.db"
    _mk_db(db)
    with pytest.raises(ValueError):
        create_snapshot(db, tier="a/b")


def test_restore_snapshot_rejects_path_outside_snapshots_dir(tmp_path: Path):
    db = tmp_path / "var" / "youos.db"
    _mk_db(db)
    # An arbitrary file outside the snapshots root must be refused, otherwise it
    # could be copied over the live DB.
    evil = tmp_path / "evil.db"
    _mk_db(evil)
    with pytest.raises(ValueError):
        restore_snapshot(db, evil)


def test_restore_snapshot_rejects_traversal_path(tmp_path: Path):
    db = tmp_path / "var" / "youos.db"
    _mk_db(db)
    snap_root = db.parent / "snapshots"
    with pytest.raises(ValueError):
        restore_snapshot(db, snap_root / ".." / ".." / "etc" / "passwd")


def test_restore_is_not_corrupted_by_a_lingering_wal(tmp_path):
    """b149: shutil.copy2 over a live WAL DB left the old -wal/-shm sidecars, so
    SQLite replayed the stale (pre-restore) frames onto the snapshot — restore
    did the OPPOSITE of its purpose. The backup-API restore must read back the
    snapshot content even with a non-empty -wal open."""
    import os
    import stat

    db = tmp_path / "var" / "youos.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(v TEXT)")
    conn.execute("INSERT INTO t VALUES ('SNAPSHOT')")
    conn.commit()
    conn.close()

    snap = create_snapshot(db, tier="manual")
    assert oct(stat.S_IMODE(os.stat(snap).st_mode)) == "0o600"  # snapshot not world-readable

    # Drift the DB AND hold a connection with a non-empty -wal open across restore.
    live = sqlite3.connect(db)
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("UPDATE t SET v='POST_SNAPSHOT'")
    live.execute("INSERT INTO t VALUES ('WAL_RESIDUE')")
    live.commit()
    restore_snapshot(db, snap)
    live.close()

    conn = sqlite3.connect(db)
    rows = [r[0] for r in conn.execute("SELECT v FROM t").fetchall()]
    conn.close()
    assert rows == ["SNAPSHOT"]  # the snapshot, not the WAL residue / post-snapshot edit
    assert oct(stat.S_IMODE(os.stat(db).st_mode)) == "0o600"  # restored DB not world-readable


class _FrozenClock:
    """Freeze data_safety's clock so the timestamp portion of every snapshot
    filename is identical — isolating whether the random suffix prevents
    collisions."""

    _FIXED = datetime(2026, 5, 31, 9, 0, 0, tzinfo=timezone.utc)

    @staticmethod
    def now(tz=None):  # noqa: ANN001 - mirrors datetime.now signature
        return _FrozenClock._FIXED


def test_two_snapshots_same_instant_do_not_collide(tmp_path: Path, monkeypatch):
    """b152: two snapshots created in the same clock-second (frozen here) must
    not overwrite each other's recovery point."""
    db = tmp_path / "var" / "youos.db"
    _mk_db(db)
    monkeypatch.setattr("app.core.data_safety.datetime", _FrozenClock)

    s1 = create_snapshot(db, tier="manual")
    s2 = create_snapshot(db, tier="manual")
    assert s1 != s2
    assert s1.exists() and s2.exists()
    assert len(list_snapshots(db)) == 2
    # no half-written temp left behind
    assert not list((db.parent / "snapshots" / "manual").glob("*.tmp"))


def test_two_restores_same_instant_keep_both_pre_restore_backups(tmp_path: Path, monkeypatch):
    """b152 (HIGH): the pre-restore backup is the only copy of the current live
    DB. Two restores in the same second must produce two distinct backups, not
    silently clobber the first (which destroyed the user's last copy)."""
    db = tmp_path / "var" / "youos.db"
    _mk_db(db)
    snap = create_snapshot(db, tier="manual")  # real timestamp, before freezing
    monkeypatch.setattr("app.core.data_safety.datetime", _FrozenClock)

    b1 = restore_snapshot(db, snap)
    b2 = restore_snapshot(db, snap)
    assert b1 != b2
    assert b1.exists() and b2.exists()


def test_restore_rejects_empty_snapshot(tmp_path: Path):
    """b152: a 0-byte snapshot (a crash mid-create) opens as a valid-but-empty
    SQLite DB; restoring it would silently wipe the live DB. Refuse it and leave
    the live DB intact."""
    db = tmp_path / "var" / "youos.db"
    _mk_db(db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO reply_pairs DEFAULT VALUES")
    conn.commit()
    conn.close()

    snap_dir = db.parent / "snapshots" / "manual"
    snap_dir.mkdir(parents=True, exist_ok=True)
    empty = snap_dir / "youos-20260531-000000-000000-aaaaaa.db"
    empty.touch()
    assert empty.stat().st_size == 0

    with pytest.raises(ValueError):
        restore_snapshot(db, empty)

    conn = sqlite3.connect(db)
    count = conn.execute("SELECT COUNT(*) FROM reply_pairs").fetchone()[0]
    conn.close()
    assert count == 1  # live DB untouched


def test_restore_rejects_snapshot_with_no_tables(tmp_path: Path):
    """b152: a non-empty but table-less DB (header only) is also a wipe vector —
    quick_check reports 'ok' on it. Refuse it before touching the live DB."""
    db = tmp_path / "var" / "youos.db"
    _mk_db(db)
    snap_dir = db.parent / "snapshots" / "manual"
    snap_dir.mkdir(parents=True, exist_ok=True)
    headerless = snap_dir / "youos-20260531-000000-000001-bbbbbb.db"
    c = sqlite3.connect(headerless)
    c.execute("PRAGMA user_version=1")  # writes a 100-byte header, zero tables
    c.commit()
    c.close()
    assert headerless.stat().st_size > 0

    with pytest.raises(ValueError):
        restore_snapshot(db, headerless)
