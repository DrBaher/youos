"""b134: credential/secret files must be owner-only (0o600), not world-readable.

A 0o644 sessions.json let any local user read the session token (the dict key,
which the auth middleware accepts verbatim as the cookie) and replay it to
bypass PIN auth. The PIN hash and API-token hashes were likewise exposed.
"""

from __future__ import annotations

import os
import sqlite3
import stat


def _mode(path) -> str:
    return oct(stat.S_IMODE(os.stat(path).st_mode))


def test_write_secret_creates_and_tightens_to_0600(tmp_path):
    from app.core.secure_io import write_secret

    f = tmp_path / "sub" / "s.json"
    write_secret(f, '{"a": 1}')
    assert _mode(f) == "0o600"  # created owner-only (parent made on the way)
    # a pre-existing world-readable file must be tightened, not left as-is.
    g = tmp_path / "g.json"
    g.write_text("x")
    os.chmod(g, 0o644)
    write_secret(g, "y")
    assert _mode(g) == "0o600"


def test_session_and_api_token_files_are_owner_only(tmp_path):
    from app.core import auth

    sp = tmp_path / "var" / "sessions.json"
    auth.save_sessions({"tok": 123.0}, sp)
    assert _mode(sp) == "0o600"

    ap = tmp_path / "var" / "api_tokens.json"
    tok = auth.add_api_token(ap)
    assert _mode(ap) == "0o600"
    assert auth.verify_api_token(tok, ap)  # the token still validates


def test_saved_config_is_owner_only(tmp_path):
    from app.core import config

    cp = tmp_path / "youos_config.yaml"
    config.save_config({"server": {"pin": "hash"}}, cp)
    assert _mode(cp) == "0o600"


def test_bootstrap_secures_var_dir_and_db(tmp_path):
    from app.db import bootstrap

    dbp = tmp_path / "var" / "youos.db"
    dbp.parent.mkdir(parents=True)
    sqlite3.connect(dbp).close()
    bootstrap._secure_db_dir(dbp)
    assert _mode(dbp.parent) == "0o700"
    assert _mode(dbp) == "0o600"
