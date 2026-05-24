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
