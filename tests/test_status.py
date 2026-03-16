"""Tests for the improved status command."""

from typer.testing import CliRunner

from scripts.youos_cli import app

runner = CliRunner()


def test_status_runs_without_db():
    """Status should run without error even with no database."""
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Status" in result.output


def test_status_shows_user_field():
    """Status should show User field."""
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "User:" in result.output


def test_status_shows_server_field():
    """Status should show Server field."""
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Server:" in result.output


def test_status_shows_port():
    """Status should show port number."""
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "8765" in result.output or "port" in result.output.lower()
