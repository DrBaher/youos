"""Tests for YouOS CLI commands."""

from typer.testing import CliRunner

from app.cli import app

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


def test_draft_rejects_invalid_mode():
    """`--mode` is a closed set — a typo is rejected, not silently passed through (b198)."""
    result = runner.invoke(app, ["draft", "hello there", "--mode", "boss"])
    assert result.exit_code != 0
    assert "boss" in result.output


def test_draft_help_shows_mode_choices():
    """The valid modes surface in --help now that it's an enum."""
    result = runner.invoke(app, ["draft", "--help"])
    assert result.exit_code == 0
    assert "work" in result.output and "personal" in result.output


def test_triage_rejects_malformed_window():
    """`--window 3days` used to become a malformed Gmail query → 0 results, no
    error. It is now rejected at parse time with a helpful message (b198)."""
    result = runner.invoke(app, ["triage", "--window", "3days"])
    assert result.exit_code != 0
    assert "not a valid window" in result.output


def test_triage_accepts_valid_window_format():
    """A well-formed window passes validation (the command may fail later for
    unrelated reasons, but never with our window error)."""
    result = runner.invoke(app, ["triage", "--window", "24h", "--account", "nobody@example.invalid"])
    assert "not a valid window" not in result.output
