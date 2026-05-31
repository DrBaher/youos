"""Tests for API-token auth used by the browser extension."""

from __future__ import annotations

from app.core.auth import (
    add_api_token,
    load_api_token_hashes,
    revoke_api_tokens,
    verify_api_token,
)


def test_add_and_verify_token(tmp_path):
    path = tmp_path / "api_tokens.json"
    token = add_api_token(path)
    assert token  # a plaintext token is returned once
    assert verify_api_token(token, path) is True


def test_wrong_token_is_rejected(tmp_path):
    path = tmp_path / "api_tokens.json"
    add_api_token(path)
    assert verify_api_token("not-the-token", path) is False


def test_empty_token_is_rejected(tmp_path):
    path = tmp_path / "api_tokens.json"
    add_api_token(path)
    assert verify_api_token("", path) is False


def test_missing_file_returns_no_hashes(tmp_path):
    path = tmp_path / "does_not_exist.json"
    assert load_api_token_hashes(path) == []
    assert verify_api_token("anything", path) is False


def test_token_is_stored_hashed_not_plaintext(tmp_path):
    path = tmp_path / "api_tokens.json"
    token = add_api_token(path)
    contents = path.read_text(encoding="utf-8")
    assert token not in contents  # only the PBKDF2 hash is persisted
    assert "pbkdf2:" in contents


def test_multiple_tokens_all_valid(tmp_path):
    path = tmp_path / "api_tokens.json"
    t1 = add_api_token(path)
    t2 = add_api_token(path)
    assert len(load_api_token_hashes(path)) == 2
    assert verify_api_token(t1, path) is True
    assert verify_api_token(t2, path) is True


def test_revoke_clears_all_tokens(tmp_path):
    path = tmp_path / "api_tokens.json"
    t = add_api_token(path)
    add_api_token(path)
    removed = revoke_api_tokens(path)
    assert removed == 2
    assert verify_api_token(t, path) is False
    assert load_api_token_hashes(path) == []


def test_token_count_is_capped(tmp_path):
    """b155: stored API tokens are capped (rotate oldest) so the file and the
    per-request verify scan stay bounded."""
    from app.core.auth import MAX_API_TOKENS

    path = tmp_path / "api_tokens.json"
    tokens = [add_api_token(path) for _ in range(MAX_API_TOKENS + 4)]
    assert len(load_api_token_hashes(path)) == MAX_API_TOKENS
    assert verify_api_token(tokens[-1], path) is True   # newest still valid
    assert verify_api_token(tokens[0], path) is False   # oldest evicted


def test_verify_runs_at_most_one_pbkdf2(tmp_path, monkeypatch):
    """b155: prefix indexing means a presented token triggers at most ONE PBKDF2,
    not one per stored token (kills the O(n) algorithmic-complexity DoS)."""
    import app.core.auth as auth

    path = tmp_path / "api_tokens.json"
    tokens = [add_api_token(path) for _ in range(5)]

    real = auth.verify_pin
    calls = {"n": 0}

    def counting(tok, h):
        calls["n"] += 1
        return real(tok, h)

    monkeypatch.setattr(auth, "verify_pin", counting)

    calls["n"] = 0
    assert verify_api_token("totally-wrong-token-value", path) is False
    assert calls["n"] <= 1  # a non-matching prefix skips the PBKDF2 entirely

    calls["n"] = 0
    assert verify_api_token(tokens[2], path) is True
    assert calls["n"] == 1  # exactly the one whose prefix matched


def test_legacy_flat_hash_list_still_verifies(tmp_path):
    """b155: a pre-b155 file (flat list of bare hash strings) must still verify."""
    import json

    from app.core.auth import get_pin_hash
    from app.core.secure_io import write_secret

    path = tmp_path / "api_tokens.json"
    write_secret(path, json.dumps([get_pin_hash("legacy-tok")]))
    assert verify_api_token("legacy-tok", path) is True
    assert verify_api_token("wrong", path) is False
