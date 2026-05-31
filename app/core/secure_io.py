"""Write credential/secret files restricted to the owner (0o600).

Session tokens, API-token hashes, and the PIN hash must not be world-readable —
a local user could otherwise read the token from a 0o644 file and replay it to
bypass PIN auth. ``Path.write_text`` inherits the umask (typically 0o644), so
secret writers go through ``write_secret`` instead.
"""

from __future__ import annotations

import os
from pathlib import Path


def write_secret(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` as an owner-only (0o600) file.

    Creates the file 0o600 from the start (no world-readable window) and also
    chmods it, since ``O_CREAT``'s mode applies only on creation — a pre-existing
    file would otherwise keep its old (possibly 0o644) permissions.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode(encoding)
    # O_NOFOLLOW: if the destination is a (pre-planted) symlink, fail with ELOOP
    # rather than following it and writing the secret through to the link target
    # (b157). Secret files are never legitimately symlinks. getattr() keeps this
    # a no-op on platforms without the flag (e.g. Windows).
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
