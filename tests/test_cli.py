"""Tests for YouOS CLI commands."""

from typer.testing import CliRunner

from scripts.youos_cli import app

runner = CliRunner()


def test_cli_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "YouOS" in result.output


def test_cli_status():
    """Status should run without error even with no database."""
    result = runner.invoke(app, ["status"])
    # May show "not found" for database but should not crash
    assert result.exit_code == 0


def test_cli_stats_no_db():
    """Stats should handle missing database gracefully."""
    result = runner.invoke(app, ["stats"])
    assert result.exit_code == 0


def test_cli_teardown_help():
    """Teardown help should show usage info."""
    result = runner.invoke(app, ["teardown", "--help"])
    assert result.exit_code == 0
    assert "Remove all YouOS user data" in result.output
