"""Write credential/secret files restricted to the owner (0o600).

Session tokens, API-token hashes, and the PIN hash must not be world-readable —
a local user could otherwise read the token from a 0o644 file and replay it to
bypass PIN auth. ``Path.write_text`` inherits the umask (typically 0o644), so
secret writers go through ``write_secret`` instead.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_secret(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically write ``text`` to ``path`` as an owner-only (0o600) file.

    Writes to a same-directory temp file (mkstemp creates it 0o600 — no
    world-readable window), fsyncs, then ``os.replace``s into place, so a
    crash/power-loss mid-write can never leave a torn or empty file behind
    (b240: a truncated config.yaml parses as valid YAML with ``server.pin``
    missing, silently disabling PIN auth; a torn OAuth token file destroys
    the refresh token). Replace also covers the b157 symlink concern that
    O_NOFOLLOW used to handle: rename swaps the destination's directory
    entry, so a pre-planted symlink is replaced, never followed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode(encoding)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp_name, path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    # mkstemp already created the file 0o600 and replace carries that mode
    # over; chmod anyway so a pre-existing looser mode can never survive.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
