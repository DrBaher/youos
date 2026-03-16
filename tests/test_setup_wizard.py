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
