# tests/core/test_config.py
"""
Tests for the Config class, particularly the _get_secret method.
"""

import logging
import os
import sys
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
    def test_automation_is_always_on_by_default(self):
        """F12: no feature flag; delivery is on unless pauseDelivery is set."""
        with patch.dict(os.environ, {}, clear=True):
            cfg = Config()
        assert cfg.AUTOMATION_ENABLED is None
        assert cfg.AUTOMATION_MODE is None
        assert cfg.automation_delivery_paused is False
        assert cfg.automation_repository_allowlist == ("@discovered",)
        assert cfg.GITOPS_DEFAULT_CREDENTIALS_SECRET == "greenkube-git-credentials"
        assert cfg.DB_RUN_MIGRATIONS is True

    @pytest.mark.parametrize(
        "env",
        [
            {"AUTOMATION_PAUSE_DELIVERY": "true"},
            {"AUTOMATION_ENABLED": "false"},
            {"AUTOMATION_ENABLED": "true", "AUTOMATION_MODE": "disabled"},
        ],
    )
    def test_pause_delivery_and_its_deprecated_aliases(self, env):
        with patch.dict(os.environ, env, clear=True):
            assert Config().automation_delivery_paused is True

    def test_repository_allowlist_accepts_json_and_comma_lists(self):
        with patch.dict(os.environ, {"AUTOMATION_REPOSITORY_ALLOWLIST": '["acme/*", "@discovered"]'}, clear=True):
            assert Config().automation_repository_allowlist == ("acme/*", "@discovered")
        with patch.dict(os.environ, {"AUTOMATION_REPOSITORY_ALLOWLIST": "acme/a, acme/b"}, clear=True):
            assert Config().automation_repository_allowlist == ("acme/a", "acme/b")
        with patch.dict(os.environ, {"AUTOMATION_REPOSITORY_ALLOWLIST": '["bad pattern"]'}, clear=True):
            with pytest.raises(ValueError, match="AUTOMATION_REPOSITORY_ALLOWLIST"):
                Config()

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


class TestChangeMeasurementWindowConfig:
    """Windows below the minimum are clamped up with a warning, never a startup failure (MEA-10 review)."""

    @pytest.mark.parametrize("value", ["3", "6.9"])
    def test_a_baseline_shorter_than_a_week_is_clamped(self, value, caplog):
        with patch.dict(os.environ, {"CHANGE_MEASUREMENT_PRE_WINDOW_DAYS": value}, clear=True):
            with caplog.at_level(logging.WARNING):
                assert Config(_env_file=None).CHANGE_MEASUREMENT_PRE_WINDOW_DAYS == 7.0
        assert "CHANGE_MEASUREMENT_PRE_WINDOW_DAYS" in caplog.text

    def test_a_zero_baseline_is_still_invalid(self):
        with patch.dict(os.environ, {"CHANGE_MEASUREMENT_PRE_WINDOW_DAYS": "0"}, clear=True):
            with pytest.raises(ValueError):
                Config(_env_file=None)

    @pytest.mark.parametrize("value", ["7", "14"])
    def test_a_baseline_of_at_least_a_week_is_accepted(self, value):
        with patch.dict(os.environ, {"CHANGE_MEASUREMENT_PRE_WINDOW_DAYS": value}, clear=True):
            assert Config(_env_file=None).CHANGE_MEASUREMENT_PRE_WINDOW_DAYS == float(value)

    @pytest.mark.parametrize("value", ["0.25", "0.0417", "1"])
    def test_accelerated_demo_windows_accept_a_baseline_of_at_least_an_hour(self, value):
        env = {"CHANGE_MEASUREMENT_PRE_WINDOW_DAYS": value, "CHANGE_MEASUREMENT_ACCELERATED": "true"}
        with patch.dict(os.environ, env, clear=True):
            assert Config(_env_file=None).CHANGE_MEASUREMENT_PRE_WINDOW_DAYS == float(value)

    @pytest.mark.parametrize("value", ["0.01", "0.04"])
    def test_accelerated_demo_windows_clamp_less_than_an_hour(self, value):
        env = {"CHANGE_MEASUREMENT_PRE_WINDOW_DAYS": value, "CHANGE_MEASUREMENT_ACCELERATED": "true"}
        with patch.dict(os.environ, env, clear=True):
            assert Config(_env_file=None).CHANGE_MEASUREMENT_PRE_WINDOW_DAYS == pytest.approx(1 / 24)

    @pytest.mark.parametrize("value", ["0.025", "1", "3", "4.9"])
    def test_a_post_window_too_short_for_a_verdict_is_clamped(self, value, caplog):
        """MEA-10: a verdict needs 5 days of aligned post-window hours; a shorter window is raised to 5 days."""
        with patch.dict(os.environ, {"CHANGE_MEASUREMENT_POST_WINDOW_DAYS": value}, clear=True):
            with caplog.at_level(logging.WARNING):
                assert Config(_env_file=None).CHANGE_MEASUREMENT_POST_WINDOW_DAYS == 5.0
        assert "CHANGE_MEASUREMENT_POST_WINDOW_DAYS" in caplog.text

    @pytest.mark.parametrize(("value", "expected"), [("0.025", 0.025), ("0.05", 0.05), ("0.01", 1 / 48)])
    def test_accelerated_post_windows_need_thirty_minutes(self, value, expected):
        env = {
            "CHANGE_MEASUREMENT_POST_WINDOW_DAYS": value,
            "CHANGE_MEASUREMENT_PRE_WINDOW_DAYS": "0.25",
            "CHANGE_MEASUREMENT_ACCELERATED": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            assert Config(_env_file=None).CHANGE_MEASUREMENT_POST_WINDOW_DAYS == pytest.approx(expected)

    def test_importing_the_config_with_a_three_day_post_window_starts(self):
        """The module-level ``config = Config()`` runs at import: a once-valid value must not stop GreenKube."""
        import subprocess

        env = {key: value for key, value in os.environ.items() if not key.startswith("CHANGE_MEASUREMENT_")}
        env["CHANGE_MEASUREMENT_POST_WINDOW_DAYS"] = "3"
        code = "from greenkube.core.config import config; print(config.CHANGE_MEASUREMENT_POST_WINDOW_DAYS)"
        result = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120, check=False
        )
        assert result.returncode == 0, result.stderr[-2000:]
        assert result.stdout.strip().splitlines()[-1] == "5.0"
        assert "CHANGE_MEASUREMENT_POST_WINDOW_DAYS" in result.stderr

    def test_acceleration_is_off_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = Config(_env_file=None)
        assert cfg.CHANGE_MEASUREMENT_ACCELERATED is False
        assert cfg.CHANGE_MEASUREMENT_PRE_WINDOW_DAYS == 7.0


class TestLedgerConfig:
    def test_chain_verification_runs_hourly_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            assert Config(_env_file=None).LEDGER_CHAIN_VERIFY_INTERVAL == "1h"

    @pytest.mark.parametrize("value", ["30s", "25h", "1d", "soon"])
    def test_invalid_chain_verification_intervals_are_rejected(self, value):
        with patch.dict(os.environ, {"LEDGER_CHAIN_VERIFY_INTERVAL": value}, clear=True):
            with pytest.raises(ValueError, match="LEDGER_CHAIN_VERIFY_INTERVAL"):
                Config(_env_file=None)


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


class TestSecretFilesForAliasedFields:
    """Secret files reach fields declared with a validation alias (e2e: scrape token ignored)."""

    def test_metrics_token_and_api_key_are_read_from_secret_files(self):
        files = {"GREENKUBE_METRICS_TOKEN": "scrape-token", "GREENKUBE_API_KEY": "bootstrap-key"}

        def read_secret(key, default=None):
            return files.get(key, default)

        with patch.dict(os.environ, {}, clear=True), patch("greenkube.core.config._read_secret", read_secret):
            cfg = Config(_env_file=None)

        assert cfg.METRICS_TOKEN == "scrape-token"
        assert cfg.API_KEY == "bootstrap-key"


def test_gpu_assumed_utilization_defaults_to_half(monkeypatch):
    monkeypatch.delenv("GPU_ASSUMED_UTILIZATION_PCT", raising=False)
    assert Config().GPU_ASSUMED_UTILIZATION_PCT == 50.0
    monkeypatch.setenv("GPU_ASSUMED_UTILIZATION_PCT", "80")
    assert Config().GPU_ASSUMED_UTILIZATION_PCT == 80.0


class TestRemovedSettings:
    """Settings removed in 0.4 are accepted, ignored and named in one warning (roadmap B.0)."""

    def test_a_removed_setting_is_accepted_and_named_once(self, caplog, monkeypatch):
        import greenkube.core.config as config_module

        monkeypatch.setattr(config_module, "_REMOVED_WARNED", set())
        with patch.dict(os.environ, {"RECOMMENDATION_KARPENTER_ENABLED": "false"}, clear=True):
            with caplog.at_level(logging.WARNING):
                Config()
                Config()
        warnings = [record.getMessage() for record in caplog.records if "Removed settings" in record.getMessage()]
        assert len(warnings) == 1
        assert "RECOMMENDATION_KARPENTER_ENABLED" in warnings[0]

    def test_no_warning_when_no_removed_setting_is_set(self, caplog, monkeypatch):
        import greenkube.core.config as config_module

        monkeypatch.setattr(config_module, "_REMOVED_WARNED", set())
        with patch.dict(os.environ, {}, clear=True):
            with caplog.at_level(logging.WARNING):
                Config()
        assert "Removed settings" not in caplog.text
