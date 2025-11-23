# tests/core/test_config.py
"""
Tests for the Config class, particularly the _get_secret method.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from greenkube.core.config import Config


class TestGetSecret:
    """Tests for the Config._get_secret method."""

    def test_get_secret_from_env_var(self):
        """Test that _get_secret falls back to environment variable when no file exists."""
        with patch.dict(os.environ, {"TEST_SECRET": "env_value"}):
            result = Config._get_secret("TEST_SECRET")
            assert result == "env_value"

    def test_get_secret_with_default(self):
        """Test that _get_secret returns default when neither file nor env var exists."""
        # Ensure the env var doesn't exist
        with patch.dict(os.environ, {}, clear=True):
            result = Config._get_secret("NONEXISTENT_SECRET", default="default_value")
            assert result == "default_value"

    def test_get_secret_from_file(self):
        """Test that _get_secret reads from file when it exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a mock secret file
            secret_dir = Path(tmpdir) / "greenkube" / "secrets"
            secret_dir.mkdir(parents=True)
            secret_file = secret_dir / "TEST_SECRET"
            secret_file.write_text("file_value\n")

            # Mock the secret path
            with patch("greenkube.core.config.os.path.exists") as mock_exists:
                mock_exists.return_value = True
                with patch("builtins.open", create=True) as mock_open:
                    mock_open.return_value.__enter__.return_value.read.return_value = "file_value\n"
                    result = Config._get_secret("TEST_SECRET")
                    assert result == "file_value"

    def test_get_secret_permission_error(self):
        """Test that _get_secret raises PermissionError with clear message when file is unreadable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a mock secret file
            secret_dir = Path(tmpdir) / "greenkube" / "secrets"
            secret_dir.mkdir(parents=True)
            secret_file = secret_dir / "TEST_SECRET"
            secret_file.write_text("secret_value")

            # Mock the file to exist but raise PermissionError on read
            with patch("greenkube.core.config.os.path.exists") as mock_exists:
                mock_exists.return_value = True
                with patch("builtins.open", side_effect=PermissionError("Permission denied")):
                    with pytest.raises(PermissionError) as exc_info:
                        Config._get_secret("TEST_SECRET")

                    # Verify the error message is clear and helpful
                    assert "exists but cannot be read due to permission denied" in str(exc_info.value)
                    assert "Please check file permissions" in str(exc_info.value)

    def test_get_secret_io_error(self):
        """Test that _get_secret raises IOError with clear message when file has I/O issues."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a mock secret file
            secret_dir = Path(tmpdir) / "greenkube" / "secrets"
            secret_dir.mkdir(parents=True)
            secret_file = secret_dir / "TEST_SECRET"
            secret_file.write_text("secret_value")

            # Mock the file to exist but raise IOError on read
            with patch("greenkube.core.config.os.path.exists") as mock_exists:
                mock_exists.return_value = True
                with patch("builtins.open", side_effect=IOError("Disk read error")):
                    with pytest.raises(IOError) as exc_info:
                        Config._get_secret("TEST_SECRET")

                    # Verify the error message is clear and helpful
                    assert "exists but cannot be read" in str(exc_info.value)
                    assert "Please check the file integrity" in str(exc_info.value)

    def test_get_secret_strips_whitespace(self):
        """Test that _get_secret strips leading/trailing whitespace from file content."""
        with patch("greenkube.core.config.os.path.exists") as mock_exists:
            mock_exists.return_value = True
            with patch("builtins.open", create=True) as mock_open:
                mock_open.return_value.__enter__.return_value.read.return_value = "  secret_value  \n"
                result = Config._get_secret("TEST_SECRET")
                assert result == "secret_value"

    def test_get_secret_file_takes_precedence_over_env(self):
        """Test that file-based secrets take precedence over environment variables."""
        with patch.dict(os.environ, {"TEST_SECRET": "env_value"}):
            with patch("greenkube.core.config.os.path.exists") as mock_exists:
                mock_exists.return_value = True
                with patch("builtins.open", create=True) as mock_open:
                    mock_open.return_value.__enter__.return_value.read.return_value = "file_value"
                    result = Config._get_secret("TEST_SECRET")
                    assert result == "file_value"
