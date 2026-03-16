"""Tests for the setup wizard dependency check."""

import sys

from scripts.setup_wizard import _check_dependencies


def test_check_dependencies_returns_bool():
    """_check_dependencies should return True or False."""
    result = _check_dependencies()
    assert isinstance(result, bool)


def test_check_dependencies_python_ok():
    """Current Python should pass the version check (we're running 3.11+)."""
    if sys.version_info >= (3, 11):
        # Should not fail on Python version
        result = _check_dependencies()
        assert isinstance(result, bool)


def test_get_user_identity_includes_internal_domains(monkeypatch):
    """_get_user_identity should return internal_domains from user input."""
    from scripts.setup_wizard import _get_user_identity

    inputs = iter(["Test User", "user@company.com", "", "Test User", "", "company.com, subsidiary.com"])
    monkeypatch.setattr("builtins.input", lambda *a, **kw: next(inputs))
    result = _get_user_identity()
    assert "internal_domains" in result
    assert "company.com" in result["internal_domains"]
    assert "subsidiary.com" in result["internal_domains"]


def test_get_user_identity_empty_domains(monkeypatch):
    """_get_user_identity should handle empty internal domains."""
    from scripts.setup_wizard import _get_user_identity

    inputs = iter(["Test User", "user@co.com", "", "Test User", "", ""])
    monkeypatch.setattr("builtins.input", lambda *a, **kw: next(inputs))
    result = _get_user_identity()
    assert result["internal_domains"] == []


def test_config_reads_explicit_internal_domains():
    """get_internal_domains reads from user.internal_domains when set."""
    from app.core.config import get_internal_domains

    config = {"user": {"internal_domains": ["acme.com", "acme.co.uk"]}}
    domains = get_internal_domains(config)
    assert "acme.com" in domains
    assert "acme.co.uk" in domains
