"""b157: native OAuth backend — token↔account identity binding + symlink/dir
hardening. The google extra isn't installed in CI, so the identity fetch is
injected via adapters._gmail_profile_email."""

from __future__ import annotations

import os
import stat
import sys
import types
from pathlib import Path

import pytest

from app.core.secure_io import write_secret
from app.ingestion import adapters


def _tok(tmp_path: Path) -> Path:
    p = tmp_path / "me@x.com.json"
    p.write_text("{}")
    return p


def test_assert_token_account_raises_on_identity_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(adapters, "_gmail_profile_email", lambda creds: "attacker@evil.com")
    adapters._VERIFIED_TOKEN_IDENTITY.clear()
    with pytest.raises(RuntimeError, match="does not match requested account"):
        adapters._assert_token_account(object(), "me@x.com", _tok(tmp_path))


def test_assert_token_account_accepts_matching_identity_case_insensitive(monkeypatch, tmp_path):
    monkeypatch.setattr(adapters, "_gmail_profile_email", lambda creds: "me@x.com")
    adapters._VERIFIED_TOKEN_IDENTITY.clear()
    adapters._assert_token_account(object(), "Me@X.com", _tok(tmp_path))  # no raise


def test_assert_token_account_skips_when_identity_undeterminable(monkeypatch, tmp_path):
    """A transient profile error / missing libs must NOT fail an otherwise-valid token."""

    def boom(creds):
        raise RuntimeError("googleapiclient not installed")

    monkeypatch.setattr(adapters, "_gmail_profile_email", boom)
    adapters._VERIFIED_TOKEN_IDENTITY.clear()
    adapters._assert_token_account(object(), "me@x.com", _tok(tmp_path))  # no raise


def test_load_credentials_refuses_wrong_account_token(monkeypatch, tmp_path):
    """End-to-end: a token whose real identity differs from the requested account
    is refused by _load_credentials before any mailbox read."""

    class _Creds:
        valid = True
        expired = False
        refresh_token = "rt"

        def to_json(self):
            return "{}"

    cred_mod = types.ModuleType("google.oauth2.credentials")
    cred_mod.Credentials = type(
        "Credentials", (), {"from_authorized_user_file": staticmethod(lambda path, scopes: _Creds())}
    )
    req_mod = types.ModuleType("google.auth.transport.requests")
    req_mod.Request = lambda: None
    for name, mod in [
        ("google", types.ModuleType("google")),
        ("google.oauth2", types.ModuleType("google.oauth2")),
        ("google.oauth2.credentials", cred_mod),
        ("google.auth", types.ModuleType("google.auth")),
        ("google.auth.transport", types.ModuleType("google.auth.transport")),
        ("google.auth.transport.requests", req_mod),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)

    monkeypatch.setattr(adapters, "_gmail_profile_email", lambda creds: "someone-else@evil.com")
    adapters._VERIFIED_TOKEN_IDENTITY.clear()

    src = adapters.NativeSource(token_dir=str(tmp_path))
    _tok(tmp_path)
    with pytest.raises(RuntimeError, match="does not match requested account"):
        src._load_credentials("me@x.com")


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW unavailable on this platform")
def test_write_secret_refuses_symlinked_destination(tmp_path):
    """b157: write_secret must not follow a pre-planted symlink and write the
    secret through to the link target."""
    victim = tmp_path / "victim.txt"
    victim.write_text("original")
    link = tmp_path / "secret.json"
    os.symlink(victim, link)
    with pytest.raises(OSError):
        write_secret(link, "SECRET_TOKEN")
    assert victim.read_text() == "original"  # link target untouched


def test_harden_token_dir_makes_dir_owner_only(tmp_path):
    token_path = tmp_path / "outside_var" / "me@x.com.json"
    adapters._harden_token_dir(token_path)
    assert token_path.parent.exists()
    assert oct(stat.S_IMODE(os.stat(token_path.parent).st_mode)) == "0o700"
