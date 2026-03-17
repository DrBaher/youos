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
    import scripts.setup_wizard as sw
    monkeypatch.setattr(sw, "_detect_gog_accounts", lambda: [])

    inputs = iter(["Test User", "user@company.com", "", "company.com, subsidiary.com"])
    monkeypatch.setattr("builtins.input", lambda *a, **kw: next(inputs))
    result = sw._get_user_identity()
    assert "internal_domains" in result
    assert "company.com" in result["internal_domains"]
    assert "subsidiary.com" in result["internal_domains"]


def test_get_user_identity_empty_domains(monkeypatch):
    """_get_user_identity should handle empty internal domains."""
    import scripts.setup_wizard as sw
    monkeypatch.setattr(sw, "_detect_gog_accounts", lambda: [])

    inputs = iter(["Test User", "user@co.com", "", ""])
    monkeypatch.setattr("builtins.input", lambda *a, **kw: next(inputs))
    result = sw._get_user_identity()
    assert result["internal_domains"] == []


def test_config_reads_explicit_internal_domains():
    """get_internal_domains reads from user.internal_domains when set."""
    from app.core.config import get_internal_domains

    config = {"user": {"internal_domains": ["acme.com", "acme.co.uk"]}}
    domains = get_internal_domains(config)
    assert "acme.com" in domains
    assert "acme.co.uk" in domains


def test_coldstart_check_passes(monkeypatch, tmp_path):
    """Cold-start check should pass with writable config and passing doctor/deps."""
    import scripts.setup_wizard as sw

    monkeypatch.setattr(sw, "CONFIG_PATH", tmp_path / "youos_config.yaml")
    monkeypatch.setattr(sw, "_check_dependencies", lambda: True)
    monkeypatch.setattr(sw, "_detect_gog_accounts", lambda: ["test@example.com"])

    class FakeDoctor:
        @staticmethod
        def run_doctor_checks():
            return True, []

    monkeypatch.setattr("app.core.doctor.run_doctor_checks", FakeDoctor.run_doctor_checks)
    assert sw._run_coldstart_check() == 0
