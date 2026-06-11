"""b253: disk-pressure pre-step — the nightly refuses to start a run that
would finish filling the disk (its first step is a full-DB snapshot), and a
merely-low disk surfaces as ok_with_warnings instead of silence."""

from __future__ import annotations

import scripts.nightly_pipeline as np


def test_verdicts(monkeypatch, tmp_path):
    monkeypatch.setattr(np, "get_var_dir", lambda: tmp_path)

    class _St:
        f_bavail = 0
        f_frsize = 1

    def fake_statvfs(path, st=None):
        return _st

    # abort below floor
    _st = type("S", (), {"f_bavail": int(1.0 * 1024**3), "f_frsize": 1})()
    monkeypatch.setattr(np.os, "statvfs", lambda p: _st)
    free, verdict = np.check_disk_pressure()
    assert verdict == "abort" and free < np.DISK_FLOOR_GB

    # warn between floor and warn level
    _st = type("S", (), {"f_bavail": int(3.0 * 1024**3), "f_frsize": 1})()
    monkeypatch.setattr(np.os, "statvfs", lambda p: _st)
    assert np.check_disk_pressure()[1] == "warn"

    # ok above warn level
    _st = type("S", (), {"f_bavail": int(50.0 * 1024**3), "f_frsize": 1})()
    monkeypatch.setattr(np.os, "statvfs", lambda p: _st)
    assert np.check_disk_pressure()[1] == "ok"


def test_statvfs_failure_never_blocks_pipeline(monkeypatch, tmp_path):
    monkeypatch.setattr(np, "get_var_dir", lambda: tmp_path)

    def boom(p):
        raise OSError("no statvfs here")

    monkeypatch.setattr(np.os, "statvfs", boom)
    free, verdict = np.check_disk_pressure()
    assert verdict == "ok"  # the doctor covers interactive diagnosis


def test_low_disk_warning_feeds_tristate_status():
    """The warn path writes an "error: ..."-prefixed results entry with
    steps True — exactly the b250 ok_with_warnings contract."""
    status, warns = np._derive_status(
        {"disk_space": True, "ingestion": True},
        {"disk_space": "error: low disk — 3.2GB free (< 5GB)", "ingestion": {}},
    )
    assert status == "ok_with_warnings"
    assert warns == ["disk_space"]
