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


class TestElectricityProviderConfig:
    """Tests for the electricity provider configuration."""

    def test_default_provider_is_electricity_maps(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = Config()
        assert cfg.ELECTRICITY_PROVIDER == "electricity_maps"

    def test_wattnet_provider_accepted(self):
        with patch.dict(os.environ, {"ELECTRICITY_PROVIDER": "WATTNET"}, clear=True):
            cfg = Config()
        assert cfg.ELECTRICITY_PROVIDER == "wattnet"

    def test_invalid_provider_rejected(self):
        with patch.dict(os.environ, {"ELECTRICITY_PROVIDER": "nonsense"}, clear=True):
            with pytest.raises(ValueError, match="ELECTRICITY_PROVIDER"):
                Config()

    def test_wattnet_fields_loaded(self):
        with patch.dict(
            os.environ,
            {
                "WATTNET_EMAIL": "user@example.com",
                "WATTNET_PASSWORD": "secret",
                "WATTNET_API_BASE_URL": "https://custom.example.com/v1",
                "WATTNET_TOKEN_SERVICE_URL": "https://custom.example.com/token",
            },
            clear=True,
        ):
            cfg = Config()
        assert cfg.WATTNET_EMAIL == "user@example.com"
        assert cfg.WATTNET_PASSWORD == "secret"
        assert cfg.WATTNET_API_BASE_URL == "https://custom.example.com/v1"
        assert cfg.WATTNET_TOKEN_SERVICE_URL == "https://custom.example.com/token"

    def test_wattnet_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = Config()
        assert cfg.WATTNET_EMAIL is None
        assert cfg.WATTNET_PASSWORD is None
        assert cfg.WATTNET_API_BASE_URL == "https://api.wattnet.eu/v1"
        assert cfg.WATTNET_TOKEN_SERVICE_URL == "https://api.wattnet.eu/token-request"


class TestAutomationRuntimeConfig:
    def test_automation_defaults_to_disabled(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = Config()
        assert not cfg.AUTOMATION_ENABLED
        assert cfg.AUTOMATION_MODE == "disabled"

    def test_inline_mode_is_normalized(self):
        with patch.dict(
            os.environ,
            {"AUTOMATION_ENABLED": "true", "AUTOMATION_MODE": "INLINE"},
            clear=True,
        ):
            cfg = Config()
        assert cfg.AUTOMATION_ENABLED
        assert cfg.AUTOMATION_MODE == "inline"

    def test_invalid_automation_mode_is_rejected(self):
        with patch.dict(os.environ, {"AUTOMATION_MODE": "cluster"}, clear=True):
            with pytest.raises(ValueError, match="AUTOMATION_MODE"):
                Config()


class TestLeaderElectionConfig:
    def test_leader_election_defaults_to_disabled(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = Config(_env_file=None)
        assert not cfg.LEADER_ELECTION_ENABLED
        assert cfg.LEADER_ELECTION_LEASE_NAME == ""
        assert cfg.LEADER_ELECTION_LEASE_NAMESPACE == ""

    def test_reads_chart_leader_election_environment(self):
        with patch.dict(
            os.environ,
            {
                "LEADER_ELECTION_ENABLED": "true",
                "LEADER_ELECTION_LEASE_NAME": "greenkube-leader",
                "LEADER_ELECTION_LEASE_NAMESPACE": "greenkube",
            },
            clear=True,
        ):
            cfg = Config(_env_file=None)
        assert cfg.LEADER_ELECTION_ENABLED
        assert cfg.LEADER_ELECTION_LEASE_NAME == "greenkube-leader"
        assert cfg.LEADER_ELECTION_LEASE_NAMESPACE == "greenkube"

    def test_reads_unprefixed_lease_environment_aliases(self):
        with patch.dict(os.environ, {"LEASE_NAME": "greenkube-leader", "LEASE_NAMESPACE": "greenkube"}, clear=True):
            cfg = Config(_env_file=None)
        assert cfg.LEADER_ELECTION_LEASE_NAME == "greenkube-leader"
        assert cfg.LEADER_ELECTION_LEASE_NAMESPACE == "greenkube"


class TestRecommendationApplyToleranceConfig:
    @pytest.mark.parametrize("value", ["-0.1", "1.01", "nan", "inf"])
    def test_invalid_apply_tolerance_is_rejected(self, value):
        with patch.dict(os.environ, {"RECOMMENDATION_APPLY_TOLERANCE": value}, clear=True):
            with pytest.raises(ValueError, match="RECOMMENDATION_APPLY_TOLERANCE"):
                Config()

    @pytest.mark.parametrize("value", ["0", "0.25", "1"])
    def test_apply_tolerance_accepts_values_in_unit_interval(self, value):
        with patch.dict(os.environ, {"RECOMMENDATION_APPLY_TOLERANCE": value}, clear=True):
            assert Config().RECOMMENDATION_APPLY_TOLERANCE == float(value)


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


class TestPrometheusStepValidation:
    """PROMETHEUS_QUERY_RANGE_STEP must be positive and divide 24 hours."""

    @pytest.mark.parametrize("step", ["0s", "0m", "0h"])
    def test_zero_step_is_rejected_without_crashing(self, step):
        with patch.dict(os.environ, {"PROMETHEUS_QUERY_RANGE_STEP": step}, clear=True):
            with pytest.raises(ValueError, match="greater than zero"):
                Config()

    def test_step_not_divisor_of_day_is_rejected(self):
        with patch.dict(os.environ, {"PROMETHEUS_QUERY_RANGE_STEP": "7m"}, clear=True):
            with pytest.raises(ValueError, match="divisor of 24 hours"):
                Config()

    def test_valid_step_is_accepted(self):
        with patch.dict(os.environ, {"PROMETHEUS_QUERY_RANGE_STEP": "5m"}, clear=True):
            assert Config().PROMETHEUS_QUERY_RANGE_STEP == "5m"


class TestGitOpsDiscoveryConfig:
    def test_defaults_match_the_operator_spec(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = Config(_env_file=None)
        assert cfg.GITOPS_DISCOVERY_INTERVAL == "5m"
        assert cfg.GITOPS_BINDING_TTL == "30m"
        assert cfg.gitops_discovery_interval_seconds == 300
        assert cfg.gitops_binding_ttl.total_seconds() == 1800

    def test_duration_strings_are_loaded(self):
        with patch.dict(
            os.environ,
            {"GITOPS_DISCOVERY_INTERVAL": "45s", "GITOPS_BINDING_TTL": "2h"},
            clear=True,
        ):
            cfg = Config(_env_file=None)
        assert cfg.gitops_discovery_interval_seconds == 45
        assert cfg.gitops_binding_ttl.total_seconds() == 7200

    @pytest.mark.parametrize("value", ["0m", "5", "1d", "soon", "-5m"])
    def test_invalid_discovery_interval_is_rejected(self, value):
        with patch.dict(os.environ, {"GITOPS_DISCOVERY_INTERVAL": value}, clear=True):
            with pytest.raises(ValueError, match="GITOPS_DISCOVERY_INTERVAL"):
                Config(_env_file=None)

    def test_blank_chart_values_fall_back_to_documented_defaults(self):
        with patch.dict(
            os.environ,
            {"GITOPS_DISCOVERY_INTERVAL": "", "GITOPS_BINDING_TTL": ""},
            clear=True,
        ):
            cfg = Config(_env_file=None)
        assert cfg.GITOPS_DISCOVERY_INTERVAL == "5m"
        assert cfg.GITOPS_BINDING_TTL == "30m"

    def test_binding_ttl_range_is_enforced(self):
        with patch.dict(os.environ, {"GITOPS_BINDING_TTL": "25h"}, clear=True):
            with pytest.raises(ValueError, match="GITOPS_BINDING_TTL"):
                Config(_env_file=None)
