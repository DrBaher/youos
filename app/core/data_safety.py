from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX (Windows); YouOS targets darwin
    fcntl = None  # type: ignore[assignment]

from app.core.atomic_io import atomic_write_json
from app.core.settings import Settings
from app.db.bootstrap import resolve_sqlite_path


def _chmod_600(path: Path) -> None:
    """Best-effort 0o600 on a DB copy — snapshots/backups are full email-DB
    copies and must not be world-readable (mirrors secure_io / bootstrap)."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

# NB: facts are stored in the `memory` table (there is no `facts` table); the
# previous "facts" entry never matched, so a real drop went undetected.
_REQUIRED_TABLES = ("reply_pairs", "draft_history", "memory")


@dataclass(slots=True)
class SafetyReport:
    db_path: str
    warnings: list[str]
    table_counts: dict[str, int]
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_path": self.db_path,
            "warnings": self.warnings,
            "table_counts": self.table_counts,
            "timestamp": self.timestamp,
        }


def _is_unsafe_path(path: Path) -> bool:
    unsafe_parts = {".Trash", "Trash"}
    return any(part in unsafe_parts for part in path.parts)


def validate_instance_paths(settings: Settings) -> None:
    db_path = resolve_sqlite_path(settings.database_url).expanduser().resolve()
    if _is_unsafe_path(db_path):
        raise RuntimeError(f"Unsafe database path detected: {db_path}")

    if settings.data_dir is not None:
        data_dir = Path(settings.data_dir).expanduser().resolve()
        expected_db = (data_dir / "var" / "youos.db").resolve()
        if db_path != expected_db:
            raise RuntimeError(
                "Database path mismatch for instance mode: "
                f"expected {expected_db}, got {db_path}. "
                "Set YOUOS_DATABASE_URL to match YOUOS_DATA_DIR/var/youos.db."
            )
        if not data_dir.exists():
            raise RuntimeError(f"Instance data directory does not exist: {data_dir}")
        if not (data_dir / "var").exists():
            raise RuntimeError(f"Missing required directory: {data_dir / 'var'}")
        if not settings.configs_dir.exists():
            raise RuntimeError(f"Missing required configs directory: {settings.configs_dir}")



def _load_prev_counts(state_path: Path) -> dict[str, int]:
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return {str(k): int(v) for k, v in data.get("table_counts", {}).items()}
    except Exception:
        return {}



def run_startup_safety_checks(settings: Settings) -> SafetyReport:
    db_path = resolve_sqlite_path(settings.database_url).expanduser().resolve()
    warnings: list[str] = []
    counts: dict[str, int] = {}

    if not db_path.exists():
        warnings.append(f"Database file not found at startup: {db_path}")
    else:
        conn = sqlite3.connect(db_path)
        try:
            existing_tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            for table in _REQUIRED_TABLES:
                if table not in existing_tables:
                    warnings.append(f"Required table missing: {table}")
                    counts[table] = 0
                    continue
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                counts[table] = int(count)
        finally:
            conn.close()

    var_dir = db_path.parent
    var_dir.mkdir(parents=True, exist_ok=True)
    state_path = var_dir / "startup_health_state.json"
    prev_counts = _load_prev_counts(state_path)

    for table in _REQUIRED_TABLES:
        prev = int(prev_counts.get(table, 0))
        curr = int(counts.get(table, 0))
        if prev > 0 and curr == 0:
            warnings.append(f"Regression detected: {table} dropped from {prev} to 0")

    timestamp = datetime.now(timezone.utc).isoformat()
    # Atomic (b241): server startup, the report API route, and the CLI
    # health-check all rewrite these concurrently; a torn baseline disarms
    # the drop-to-zero regression detector for the next run.
    atomic_write_json(state_path, {"timestamp": timestamp, "table_counts": counts})

    report = SafetyReport(
        db_path=str(db_path),
        warnings=warnings,
        table_counts=counts,
        timestamp=timestamp,
    )
    atomic_write_json(var_dir / "startup_safety_report.json", report.to_dict())
    return report


def _snapshot_root(db_path: Path) -> Path:
    return db_path.parent / "snapshots"


_TIER_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# The only tiers create_snapshot will write. prune_snapshots knows the retention
# for exactly these; an arbitrary tier (e.g. via the create-snapshot route's
# query param) would write full-DB-copy snapshots that prune never reclaimed →
# unbounded disk-fill (b158). _DEFAULT_STRAY_KEEP bounds any pre-existing stray
# tier dir so prune reclaims it too.
_SNAPSHOT_TIERS = ("hourly", "daily", "manual")
_DEFAULT_STRAY_KEEP = 5
# Pre-restore backups kept next to the live DB (b243): rollback targets for
# recent restores only; older ones were never reclaimed before.
_PRE_RESTORE_KEEP = 10


def _validate_tier(tier: str) -> str:
    """Reject tier values that aren't a single safe path component.

    Without this, ``tier="../../../tmp/x"`` would escape the snapshots dir and
    write a full DB copy to an arbitrary location (path traversal).
    """
    if not _TIER_RE.match(tier):
        raise ValueError(f"Invalid snapshot tier: {tier!r} (expected alphanumerics, '-' or '_')")
    return tier


# Bounded waits (b243): a restore stalled behind a wedged writer used to hold
# the flock FOREVER (Python's sqlite3 backup retries SQLITE_BUSY indefinitely);
# the nightly's create_snapshot — its FIRST step — then blocked on the flock,
# silently hanging the whole pipeline for days. Both waits now fail loudly.
_LOCK_ACQUIRE_DEADLINE_SECONDS = 120.0
_BACKUP_DEADLINE_SECONDS = 120.0


@contextmanager
def _snapshot_lock(db_path: Path):
    """Cross-process exclusive lock serializing ALL snapshot writers — create,
    restore, prune (b158).

    Two restores (or a restore racing the nightly's create/prune) otherwise run
    concurrently: the restore handler is a sync def on the AnyIO threadpool (true
    parallelism), and the nightly is a separate process, so a threading.Lock is
    insufficient. An ``fcntl.flock(LOCK_EX)`` on ``var/.snapshot.lock`` makes
    take-backup-then-overwrite atomic vs. any other snapshot writer. Degrades to
    a no-op where fcntl is unavailable (non-POSIX).

    Acquisition is BOUNDED (b243): LOCK_NB + retry until
    ``_LOCK_ACQUIRE_DEADLINE_SECONDS``, then TimeoutError — a holder wedged
    mid-restore must fail the caller loudly, not hang the nightly forever."""
    if fcntl is None:  # pragma: no cover - non-POSIX
        yield
        return
    lock_dir = db_path.parent
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    fd = os.open(lock_dir / ".snapshot.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + _LOCK_ACQUIRE_DEADLINE_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "another snapshot operation (create/restore/prune) has held "
                        f"var/.snapshot.lock for over {_LOCK_ACQUIRE_DEADLINE_SECONDS:.0f}s "
                        "— refusing to queue behind it"
                    ) from None
                time.sleep(0.5)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextmanager
def snapshot_lock(db_path: Path):
    """Public alias of the snapshot writers' lock, for OTHER whole-DB
    operations (schema bootstrap, retention VACUUM) that must not interleave
    with a snapshot create/restore/prune (b243)."""
    with _snapshot_lock(db_path):
        yield


def _deadline_progress(what: str):
    """sqlite3 ``backup(progress=...)`` callback that aborts a copy stuck
    behind a busy writer. CPython invokes it every iteration — including
    SQLITE_BUSY ones — so raising here bounds an otherwise-infinite retry
    loop (b243)."""
    deadline = time.monotonic() + _BACKUP_DEADLINE_SECONDS
    def _cb(status: int, remaining: int, total: int) -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"{what} did not complete within {_BACKUP_DEADLINE_SECONDS:.0f}s "
                "(database busy — a writer is holding the live DB)"
            )
    return _cb


def _ensure_within(root: Path, candidate: Path) -> Path:
    """Resolve ``candidate`` and confirm it lives inside ``root``.

    Guards the restore path so an attacker can't point it at an arbitrary file
    on disk (which would otherwise overwrite the live DB) or write outside the
    managed snapshots directory.
    """
    resolved = candidate.expanduser().resolve()
    root_resolved = root.expanduser().resolve()
    if not resolved.is_relative_to(root_resolved):
        raise ValueError(f"Path escapes the snapshots directory: {candidate}")
    return resolved


def _utc_stamp() -> str:
    """Timestamp with sub-second precision for snapshot filenames.

    The old ``%Y%m%d-%H%M%S`` (1-second granularity) let two snapshots — or two
    pre-restore backups — created in the same second collide on filename and
    silently overwrite each other. A restore completes in ~3ms, so two
    back-to-back restores land in the same second by default and the pre-restore
    backup (the only copy of the live DB) was being destroyed (b152)."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


def _fsync_path(path: Path) -> None:
    """Best-effort fsync so a complete snapshot survives a crash (durability)."""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _atomic_db_copy(db_path: Path, final_path: Path) -> None:
    """Write a WAL-consistent, integrity-checked copy of ``db_path`` to
    ``final_path`` *atomically*.

    The copy is written to an ``O_EXCL`` temp in the same directory, verified
    with ``quick_check``, fsynced, then ``os.replace``'d into ``final_path`` —
    so only a complete, valid DB ever appears under the final name. A crash
    mid-write leaves only a ``.tmp`` (which the ``youos-*.db`` glob ignores)
    rather than a 0-byte file that ``restore`` would later treat as a valid,
    empty DB and silently restore over live data. ``O_EXCL`` also means a
    symlink planted at the temp path raises rather than redirecting the copy."""
    tmp_path = final_path.with_name(final_path.name + ".tmp")
    fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA wal_checkpoint(FULL)")
            dst = sqlite3.connect(tmp_path)
            try:
                conn.backup(dst, pages=256, progress=_deadline_progress("snapshot copy"))
                ok = dst.execute("PRAGMA quick_check").fetchone()
                if not ok or ok[0] != "ok":
                    raise sqlite3.DatabaseError(f"snapshot failed integrity check: {ok}")
            finally:
                dst.close()
        finally:
            conn.close()
        _fsync_path(tmp_path)
        os.replace(tmp_path, final_path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)  # never leave a half-written temp behind
        except OSError:
            pass
        raise
    _chmod_600(final_path)  # the copy is a full email DB


def _assert_restorable(snapshot_path: Path) -> None:
    """Refuse to restore a snapshot that would silently wipe the live DB.

    A 0-byte file (e.g. left by a crash mid-create before atomic writes existed,
    or any externally-truncated file) opens as a *valid but empty* SQLite DB —
    ``quick_check`` reports 'ok' and restoring it discards all real data. So we
    reject an empty file, a file that fails ``quick_check``, and a DB with no
    user tables, BEFORE touching the live DB. We deliberately do NOT require the
    full ``_REQUIRED_TABLES`` set — a legitimate snapshot may predate a table —
    only that the snapshot carries real schema, which the wipe-case never does."""
    if snapshot_path.stat().st_size == 0:
        raise ValueError(f"Refusing to restore an empty snapshot: {snapshot_path}")
    conn = sqlite3.connect(snapshot_path)
    try:
        ok = conn.execute("PRAGMA quick_check").fetchone()
        if not ok or ok[0] != "ok":
            raise ValueError(f"Snapshot failed integrity check ({ok}): {snapshot_path}")
        tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        if not tables:
            raise ValueError(f"Refusing to restore a snapshot with no tables: {snapshot_path}")
    finally:
        conn.close()


def create_snapshot(db_path: Path, *, tier: str = "manual") -> Path:
    _validate_tier(tier)
    if tier not in _SNAPSHOT_TIERS:
        raise ValueError(f"Unknown snapshot tier: {tier!r} (expected one of {_SNAPSHOT_TIERS})")
    with _snapshot_lock(db_path):
        out_dir = _snapshot_root(db_path) / tier
        out_dir.mkdir(parents=True, exist_ok=True)
        # Sub-second stamp + random suffix so same-second snapshots never collide;
        # _atomic_db_copy makes the write atomic + integrity-checked.
        out_path = out_dir / f"youos-{_utc_stamp()}-{os.urandom(3).hex()}.db"
        _atomic_db_copy(db_path, out_path)
    return out_path


def _load_snapshot_retention(
    *,
    keep_hourly: int | None,
    keep_daily: int | None,
    keep_manual: int | None,
) -> tuple[int, int, int]:
    """Resolve retention limits from explicit args > config > historical defaults.

    Historical defaults: 72 hourly, 30 daily, 50 manual — preserved exactly
    so existing callers that don't pass kwargs see identical behaviour.
    YAML override key: ``snapshots: {keep_hourly: N, keep_daily: N, keep_manual: N}``.
    """
    cfg: dict[str, Any] = {}
    if keep_hourly is None or keep_daily is None or keep_manual is None:
        try:
            from app.core.config import load_config

            raw = load_config() or {}
            cfg = raw.get("snapshots", {}) if isinstance(raw, dict) else {}
            if not isinstance(cfg, dict):
                cfg = {}
        except Exception:
            cfg = {}

    def _resolve(arg: int | None, key: str, default: int) -> int:
        if arg is not None:
            return int(arg)
        val = cfg.get(key)
        if isinstance(val, int) and val >= 0:
            return val
        return default

    return (
        _resolve(keep_hourly, "keep_hourly", 72),
        _resolve(keep_daily, "keep_daily", 30),
        _resolve(keep_manual, "keep_manual", 50),
    )


def prune_snapshots(
    db_path: Path,
    *,
    keep_hourly: int | None = None,
    keep_daily: int | None = None,
    keep_manual: int | None = None,
) -> dict[str, int]:
    """Prune snapshots beyond per-tier retention limits.

    Returns a per-tier count of files removed so callers (the nightly,
    the CLI) can report what they did. Limits resolve from explicit
    kwargs > YAML config (``snapshots.keep_*``) > historical defaults
    (72 / 30 / 50). Existing callers that don't pass kwargs see exactly
    the same behaviour they always did.
    """
    hourly, daily, manual = _load_snapshot_retention(
        keep_hourly=keep_hourly, keep_daily=keep_daily, keep_manual=keep_manual,
    )
    keep_by_tier = {"hourly": hourly, "daily": daily, "manual": manual}
    root = _snapshot_root(db_path)
    removed: dict[str, int] = {"hourly": 0, "daily": 0, "manual": 0}
    with _snapshot_lock(db_path):
        # Iterate EVERY tier dir (not just the three known ones) so a stray tier
        # — e.g. a pre-existing one created before the create-time allowlist —
        # is still reclaimed under a default cap instead of growing forever.
        for tier_dir in sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []:
            tier = tier_dir.name
            keep = keep_by_tier.get(tier, _DEFAULT_STRAY_KEEP)
            pairs = _snapshot_files_by_mtime(tier_dir.glob("youos-*.db"))
            for old, _mtime in pairs[keep:]:
                old.unlink(missing_ok=True)
                removed[tier] = removed.get(tier, 0) + 1
        # Pre-restore backups (full DB copies dropped next to the live DB by
        # every restore) were never reclaimed by any tier loop — unbounded
        # growth on repeated restores (b243). Keep the newest few; they only
        # matter as rollback targets for RECENT restores.
        pre = _snapshot_files_by_mtime(db_path.parent.glob("youos.pre-restore-*.db"))
        for old, _mtime in pre[_PRE_RESTORE_KEEP:]:
            old.unlink(missing_ok=True)
            removed["pre_restore"] = removed.get("pre_restore", 0) + 1
    return removed


def _snapshot_files_by_mtime(paths) -> list[tuple[Path, float]]:
    """Return (path, mtime) pairs newest-first, dropping any file that vanished.

    Materializing the stat BEFORE sorting (vs. ``stat`` inside the sort key)
    avoids a FileNotFoundError when a concurrent prune/restore unlinks a file
    between the glob and the stat (b158)."""
    pairs: list[tuple[Path, float]] = []
    for p in paths:
        try:
            pairs.append((p, p.stat().st_mtime))
        except OSError:
            continue  # vanished mid-iteration — skip
    pairs.sort(key=lambda t: t[1], reverse=True)
    return pairs


def list_snapshots(db_path: Path) -> list[Path]:
    root = _snapshot_root(db_path)
    if not root.exists():
        return []
    return [p for p, _ in _snapshot_files_by_mtime(root.glob("*/*.db"))]


def restore_snapshot(db_path: Path, snapshot_path: Path, *, dry_run: bool = False) -> Path:
    # Only allow restoring from inside the managed snapshots directory. Without
    # this, an arbitrary path would let a caller overwrite the live DB with any
    # readable file on disk.
    snapshot_path = _ensure_within(_snapshot_root(db_path), snapshot_path)
    if not snapshot_path.exists():
        raise FileNotFoundError(f"Snapshot not found: {snapshot_path}")

    # Validate the snapshot BEFORE we overwrite the live DB — a 0-byte/corrupt
    # snapshot would otherwise silently wipe real data.
    _assert_restorable(snapshot_path)

    # Sub-second + random name so a 2nd restore in the same second can't clobber
    # this backup — it's the user's ONLY copy of the current live DB.
    backup_path = db_path.parent / f"youos.pre-restore-{_utc_stamp()}-{os.urandom(4).hex()}.db"
    if dry_run:
        return backup_path

    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Hold the cross-process snapshot lock across take-backup-THEN-overwrite so a
    # concurrent restore can't capture this restore's intermediate content as its
    # "original" backup (false-confidence rollback target), and the nightly's
    # create/prune can't race the live-DB overwrite (b158).
    with _snapshot_lock(db_path):
        if db_path.exists():
            # Atomic, O_EXCL, WAL-consistent: never silently overwrite an existing
            # pre-restore backup, and never truncate it on a crash mid-write.
            _atomic_db_copy(db_path, backup_path)

        # Restore via the SQLite backup API, NOT shutil.copy2. Copying the snapshot
        # over a live WAL DB leaves the old youos.db-wal/-shm sidecars in place, and
        # SQLite then replays those stale (pre-restore) frames onto the snapshot on
        # the next open — silently discarding the restore or producing a malformed
        # DB. Writing the snapshot content THROUGH a real connection lets SQLite
        # handle WAL correctly; checkpoint(TRUNCATE) then folds + clears it.
        snap = sqlite3.connect(snapshot_path)
        try:
            live = sqlite3.connect(db_path)
            try:
                snap.backup(live, pages=256, progress=_deadline_progress("snapshot restore"))
                live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                live.commit()
            finally:
                live.close()
        finally:
            snap.close()
        _chmod_600(db_path)  # don't leave the restored DB world-readable
    return backup_path
