"""Tests for the top-level GreenKube CLI."""

from typer.testing import CliRunner

from greenkube import __version__
from greenkube.cli import app

runner = CliRunner()


def test_help_lists_available_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "start" in result.stdout
    assert "demo" in result.stdout
    assert "version" in result.stdout
    assert "report" not in result.stdout
    assert "recommend" not in result.stdout


def test_unknown_command_shows_help():
    result = runner.invoke(app, ["not-a-command"])
    assert result.exit_code != 0


def test_version_command():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout
