"""b240: write_secret is atomic+durable; auth survives a damaged config.

A torn config.yaml (the Mac crashed mid-write) can parse as valid YAML with
``server.pin`` missing — silently disabling PIN auth — or leave ``server:``
null, crashing the auth middleware. The writer is now temp+fsync+os.replace
so a torn file can never exist, and is_auth_enabled tolerates a non-dict
``server`` section.
"""

from __future__ import annotations

import os
import stat

import pytest

from app.core.auth import is_auth_enabled
from app.core.secure_io import write_secret


def test_write_secret_replaces_atomically_and_keeps_0600(tmp_path):
    f = tmp_path / "config.yaml"
    write_secret(f, "first: 1\n")
    os.chmod(f, 0o644)  # loosened out-of-band
    write_secret(f, "second: 2\n")
    assert f.read_text() == "second: 2\n"
    assert oct(stat.S_IMODE(os.stat(f).st_mode)) == "0o600"
    # no temp droppings
    assert [p.name for p in tmp_path.iterdir()] == ["config.yaml"]


def test_write_secret_failed_write_leaves_original_intact(tmp_path, monkeypatch):
    f = tmp_path / "config.yaml"
    write_secret(f, "original")

    real_write = os.write

    def exploding_write(fd, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "write", exploding_write)
    with pytest.raises(OSError):
        write_secret(f, "X" * 1024)
    monkeypatch.setattr(os, "write", real_write)

    assert f.read_text() == "original"  # destination never touched
    assert [p.name for p in tmp_path.iterdir()] == ["config.yaml"]  # temp cleaned up


def test_is_auth_enabled_tolerates_damaged_server_section():
    assert is_auth_enabled({"server": {"pin": "pbkdf2:..."}}) is True
    assert is_auth_enabled({"server": {"pin": ""}}) is False
    assert is_auth_enabled({"server": {}}) is False
    assert is_auth_enabled({}) is False
    # damaged shapes a torn/hand-edited YAML can produce — must not raise
    assert is_auth_enabled({"server": None}) is False
    assert is_auth_enabled({"server": "garbage"}) is False
    assert is_auth_enabled({"server": ["list"]}) is False
