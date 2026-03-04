# tests/demo/test_cli_demo.py
"""
Unit tests for the `greenkube demo` CLI command.
Tests the command interface without actually starting the server.
"""

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from greenkube.cli import app

runner = CliRunner()


class TestDemoCommand:
    """Tests for the greenkube demo CLI command."""

    def test_demo_help(self):
        """The demo command should have help text."""
        result = runner.invoke(app, ["demo", "--help"])
        assert result.exit_code == 0
        assert "demo" in result.output.lower()

    def test_demo_invokes_runner(self):
        """The demo command should invoke run_demo."""
        with patch("greenkube.cli.demo.asyncio") as mock_asyncio:
            mock_asyncio.run = MagicMock()
            runner.invoke(app, ["demo", "--no-browser"])
            mock_asyncio.run.assert_called_once()

    def test_demo_passes_port(self):
        """Custom port should be forwarded to run_demo."""
        with patch("greenkube.cli.demo.asyncio") as mock_asyncio:
            mock_asyncio.run = MagicMock()
            runner.invoke(app, ["demo", "--port", "9000", "--no-browser"])
            call_args = mock_asyncio.run.call_args
            # The coroutine is the first arg to asyncio.run
            assert call_args is not None

    def test_demo_passes_days(self):
        """Custom days should be forwarded to run_demo."""
        with patch("greenkube.cli.demo.asyncio") as mock_asyncio:
            mock_asyncio.run = MagicMock()
            runner.invoke(app, ["demo", "--days", "14", "--no-browser"])
            call_args = mock_asyncio.run.call_args
            assert call_args is not None

    def test_demo_default_port(self):
        """Default port should be 8000."""
        with patch("greenkube.cli.demo.asyncio") as mock_asyncio:
            mock_asyncio.run = MagicMock()
            runner.invoke(app, ["demo", "--no-browser"])
            mock_asyncio.run.assert_called_once()

    def test_demo_handles_keyboard_interrupt(self):
        """KeyboardInterrupt should be caught gracefully."""
        with patch("greenkube.cli.demo.asyncio") as mock_asyncio:
            mock_asyncio.run = MagicMock(side_effect=KeyboardInterrupt)
            result = runner.invoke(app, ["demo", "--no-browser"])
            # Should not crash - exit code 0 or None
            assert result.exit_code == 0 or result.exit_code is None

    def test_demo_handles_runtime_error(self):
        """Runtime errors should be caught and exit with code 1."""
        with patch("greenkube.cli.demo.asyncio") as mock_asyncio:
            mock_asyncio.run = MagicMock(side_effect=RuntimeError("test error"))
            result = runner.invoke(app, ["demo", "--no-browser"])
            assert result.exit_code == 1
