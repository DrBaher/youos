"""b245: per-account identity-binding parity gaps from the pass-8 audit.

P8-1: gws with NO credentials map and MULTIPLE ingestion accounts silently
read/drafted the ambient mailbox for every account (reads use userId:"me",
so nothing downstream caught it). P8-3: an empty account with a configured
map also fell through to ambient. P8-4: native token files were keyed by
the verbatim account string while the identity assert compared normalized.
P8-5: an empty --account value must never reach a gog/gws spawn.
"""

from __future__ import annotations

import pytest

from app.ingestion import adapters
from app.ingestion.adapters import _resolve_gws_credentials_file, require_account_argv


def test_gws_no_map_multi_account_refuses(monkeypatch):
    monkeypatch.setattr(adapters, "_multiple_ingestion_accounts", lambda: True)
    with pytest.raises(ValueError, match="multiple ingestion accounts"):
        _resolve_gws_credentials_file("b@y.com", {})


def test_gws_no_map_single_account_still_ambient(monkeypatch):
    monkeypatch.setattr(adapters, "_multiple_ingestion_accounts", lambda: False)
    assert _resolve_gws_credentials_file("a@x.com", {}) is None


def test_gws_empty_account_with_map_refuses():
    with pytest.raises(ValueError, match="empty account"):
        _resolve_gws_credentials_file("", {"a@x.com": "/c1"})
    with pytest.raises(ValueError, match="empty account"):
        _resolve_gws_credentials_file(None, {"a@x.com": "/c1"})


def test_gws_mapped_lookup_still_normalized():
    assert _resolve_gws_credentials_file("  A@X.COM ", {"a@x.com": "/c1"}) == "/c1"


def test_require_account_argv_blocks_empty_value():
    with pytest.raises(ValueError, match="empty --account"):
        require_account_argv(["gog", "gmail", "labels", "list", "--account", "", "--json"])
    with pytest.raises(ValueError, match="empty --account"):
        require_account_argv(["gog", "calendar", "freebusy", "--account", "   "])
    with pytest.raises(ValueError, match="empty --account"):
        require_account_argv(["gog", "x", "--account"])  # value missing entirely
    require_account_argv(["gog", "gmail", "labels", "list", "--account", "a@x.com"])  # fine
    require_account_argv(["gws", "gmail", "search"])  # no --account at all: ambient path, fine


def test_native_token_path_is_normalized_with_legacy_fallback(tmp_path):
    src = adapters.NativeSource(token_dir=tmp_path)

    # Same file for case/whitespace variants of the account.
    assert src._token_path("A@X.com") == src._token_path("  a@x.com ")
    assert src._token_path("a@x.com").name == "a@x.com.json"

    # Legacy verbatim-named token file keeps working. (Whitespace, not case:
    # APFS is case-insensitive, so a case-only difference resolves to the same
    # file anyway — Linux CI exercises the case-sensitive path via normalize.)
    legacy = tmp_path / " a@x.com .json"
    legacy.write_text("{}")
    assert src._token_path(" a@x.com ") == legacy
    # ...until a normalized file exists, which then wins.
    (tmp_path / "a@x.com.json").write_text("{}")
    assert src._token_path(" a@x.com ").name == "a@x.com.json"
