"""Cross-process advisory locks (``fcntl.flock``) for heavyweight singletons.

A ``threading.Lock`` only guards concurrency *within a single process*. The
nightly pipeline runs as a separate launchd process from the API server, so
guarding the ``/trigger-autoresearch`` route with a threading.Lock alone does
NOT stop the launchd 01:00 run from racing an API-triggered run on the shared
config files (torn ``_write_yaml`` reads) and the git index (a swallowed
``index.lock`` abort silently drops a kept config change). These helpers use
``fcntl.flock`` on a lockfile under ``var/`` so the guarantee holds across
processes.
"""

from __future__ import annotations

import fcntl
import os

from app.core.settings import get_var_dir

# Canonical lock names, shared so the holder and any consulters agree on the file.
NIGHTLY_PIPELINE_LOCK = "nightly_pipeline.lock"

# fds for process-lifetime singleton locks, retained so they aren't garbage
# collected (which would drop the flock); the OS releases them on process exit.
_HELD_FDS: dict[str, int] = {}


def _lock_path(name: str) -> str:
    var_dir = get_var_dir()
    var_dir.mkdir(parents=True, exist_ok=True)
    return str(var_dir / name)


def acquire_singleton(name: str) -> bool:
    """Acquire a process-lifetime exclusive lock; return ``False`` if another
    process already holds it.

    Non-blocking: a second holder bails immediately rather than queueing behind
    a multi-hour job. The lock is held until this process exits (the fd is kept
    in a module registry and released by the OS on exit), so callers don't need
    to release it explicitly.
    """
    if name in _HELD_FDS:
        return True
    fd = os.open(_lock_path(name), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    _HELD_FDS[name] = fd
    return True


def is_locked(name: str) -> bool:
    """Best-effort check whether another process currently holds ``name``.

    There's an inherent TOCTOU window (the lock can be taken right after this
    returns), so this is only for honest user feedback — the real guarantee is
    that the singleton holder calls :func:`acquire_singleton` and bails itself.
    """
    if name in _HELD_FDS:
        return True
    fd = os.open(_lock_path(name), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)
