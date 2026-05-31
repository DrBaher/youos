"""Tests for the cross-process nightly pipeline lock + atomic config write (b164)."""

import multiprocessing as mp

import yaml


def _hold_lock(lockdir, started, release):
    """Child process: acquire the singleton pipeline lock, signal, hold until told."""
    import os

    os.environ["YOUOS_DATA_DIR"] = str(lockdir)
    import app.core.settings as s

    s.get_settings.cache_clear()
    from app.core.proc_lock import NIGHTLY_PIPELINE_LOCK, acquire_singleton

    got = acquire_singleton(NIGHTLY_PIPELINE_LOCK)
    started.set()
    if got:
        release.wait(timeout=10)
    # child exits -> the OS auto-releases the flock


def test_pipeline_lock_is_cross_process(tmp_path, monkeypatch):
    """A second process must not be able to acquire the lock while another holds it."""
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    import app.core.settings as s

    s.get_settings.cache_clear()
    from app.core.proc_lock import (
        _HELD_FDS,
        NIGHTLY_PIPELINE_LOCK,
        acquire_singleton,
        is_locked,
    )

    started = mp.Event()
    release = mp.Event()
    proc = mp.Process(target=_hold_lock, args=(tmp_path, started, release))
    proc.start()
    try:
        assert started.wait(timeout=10), "child never acquired the lock"
        # While the child holds it, this process must see it locked and fail to take it.
        assert is_locked(NIGHTLY_PIPELINE_LOCK) is True
        assert acquire_singleton(NIGHTLY_PIPELINE_LOCK) is False
        # A failed acquire must not register an fd (else a later call short-circuits True).
        assert NIGHTLY_PIPELINE_LOCK not in _HELD_FDS
    finally:
        release.set()
        proc.join(timeout=10)

    # Once the holder exits, the lock is free again.
    assert is_locked(NIGHTLY_PIPELINE_LOCK) is False


def test_write_yaml_is_atomic(tmp_path):
    """_write_yaml round-trips, overwrites cleanly, and leaves no temp files behind."""
    from app.autoresearch.mutator import _write_yaml

    target = tmp_path / "cfg.yaml"
    _write_yaml(target, {"a": 1, "b": [1, 2, 3]})
    assert yaml.safe_load(target.read_text()) == {"a": 1, "b": [1, 2, 3]}

    # The real autoresearch path overwrites an existing config; still atomic.
    _write_yaml(target, {"a": 2})
    assert yaml.safe_load(target.read_text()) == {"a": 2}

    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".cfg.yaml.")]
    assert leftovers == []
