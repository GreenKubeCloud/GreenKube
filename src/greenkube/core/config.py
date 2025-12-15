# src/greenkube/core/config.py

import logging
import os
import re
from datetime import timedelta

from dotenv import load_dotenv

# Import datacenter PUE profiles
from greenkube.data.datacenter_pue_profiles import DATACENTER_PUE_PROFILES

# Load environment variables from a .env file located in the project root
dotenv_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env")
load_dotenv(dotenv_path=dotenv_path)


class Config:
    """
    Handles the application's configuration by loading values from environment variables.
    """

    def __init__(self):
        # --- Database variables ---
        self.ELECTRICITY_MAPS_TOKEN = self._get_secret("ELECTRICITY_MAPS_TOKEN")

        # --- ELASTICSEARCH VARIABLES ---
        self.ELASTICSEARCH_USER = self._get_secret("ELASTICSEARCH_USER")
        self.ELASTICSEARCH_PASSWORD = self._get_secret("ELASTICSEARCH_PASSWORD")

        # -- Prometheus variables ---
        self.PROMETHEUS_BEARER_TOKEN = self._get_secret("PROMETHEUS_BEARER_TOKEN")
        self.PROMETHEUS_USERNAME = self._get_secret("PROMETHEUS_USERNAME")
        self.PROMETHEUS_PASSWORD = self._get_secret("PROMETHEUS_PASSWORD")

    @staticmethod
    def _get_secret(key: str, default: str = None) -> str:
        """
        Retrieves a secret from a file (Docker secret/volume) or falls back to environment variable.

        Raises:
            PermissionError: If the secret file exists but cannot be read due to permissions.
            IOError: If the secret file exists but cannot be read due to I/O errors.
        """
        # Check for secret file first (mounted volume)
        secret_file = f"/etc/greenkube/secrets/{key}"
        if os.path.exists(secret_file):
            try:
                with open(secret_file, "r") as f:
                    value = f.read().strip()
                    logging.getLogger(__name__).debug(f"Loaded secret '{key}' from {secret_file}")
                    return value
            except PermissionError as e:
                # Fail fast with clear error message for permission issues
                raise PermissionError(
                    f"Secret file '{secret_file}' exists but cannot be read due to permission denied. "
                    f"Please check file permissions or run with appropriate privileges."
                ) from e
            except (IOError, OSError) as e:
                # Fail fast for other I/O errors (disk issues, etc.)
                raise IOError(
                    f"Secret file '{secret_file}' exists but cannot be read: {e}. "
                    f"Please check the file integrity and system resources."
                ) from e
        # Fallback to environment variable
        return os.getenv(key, default)

    # --- Default variables ---
    DEFAULT_COST = 0.0

    # CLOUD_PROVIDER and DEFAULT_PUE are provided as properties so their
    # values are resolved at access time (reading environment variables and
    # DATACENTER_PUE_PROFILES). This avoids binding a stale value at import
    # time and lets callers create calculators after changing env vars.
    @property
    def CLOUD_PROVIDER(self) -> str:
        return os.getenv("CLOUD_PROVIDER", "aws").lower()

    @property
    def DEFAULT_PUE(self) -> float:
        # Resolve profile key dynamically
        profile_key = f"default_{self.CLOUD_PROVIDER}"
        pue = DATACENTER_PUE_PROFILES.get(profile_key)
        if pue is None:
            fallback = float(os.getenv("DEFAULT_PUE", 1.3))
            logging.getLogger(__name__).warning(
                "Unknown CLOUD_PROVIDER '%s' - falling back to DEFAULT_PUE=%s",
                self.CLOUD_PROVIDER,
                fallback,
            )
            return fallback
        return pue

    def get_pue_for_provider(self, provider: str) -> float:
        """
        Retrieves the PUE for a specific cloud provider.
        """
        if not provider:
            return self.DEFAULT_PUE

        profile_key = f"default_{provider.lower()}"
        pue = DATACENTER_PUE_PROFILES.get(profile_key)
        if pue is None:
            return self.DEFAULT_PUE
        return pue

    DEFAULT_ZONE = os.getenv("DEFAULT_ZONE", "FR")
    DEFAULT_INTENSITY = float(os.getenv("DEFAULT_INTENSITY", 500))
    JOULES_PER_KWH = 3.6e6

    # --- Logging variables ---
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    # --- Database variables ---
    DB_TYPE = os.getenv("DB_TYPE", "sqlite")
    DB_PATH = os.getenv("DB_PATH", "greenkube_data.db")
    DB_CONNECTION_STRING = os.getenv("DB_CONNECTION_STRING")
    DB_SCHEMA = os.getenv("DB_SCHEMA", "public")

    # --- ELASTICSEARCH VARIABLES ---
    ELASTICSEARCH_HOSTS = os.getenv("ELASTICSEARCH_HOSTS", "http://localhost:9200")
    ELASTICSEARCH_VERIFY_CERTS = os.getenv("ELASTICSEARCH_VERIFY_CERTS", "True").lower() in (
        "true",
        "1",
        "t",
        "y",
        "yes",
    )
    ELASTICSEARCH_INDEX_NAME = os.getenv("ELASTICSEARCH_INDEX_NAME", "carbon_intensity")

    # -- Prometheus variables ---
    # Default to empty so the application attempts discovery when no URL is
    # provided by environment/config. This avoids binding a potentially
    # stale in-cluster DNS name at import time.
    PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "")
    PROMETHEUS_QUERY_RANGE_STEP = os.getenv("PROMETHEUS_QUERY_RANGE_STEP", "5m")
    PROMETHEUS_QUERY_RANGE_MAX_SAMPLES = int(
        os.getenv("PROMETHEUS_QUERY_RANGE_MAX_SAMPLES", "10000")
    )  # Max data points in a range query
    PROMETHEUS_VERIFY_CERTS = os.getenv("PROMETHEUS_VERIFY_CERTS", "True").lower() in (
        "true",
        "1",
        "t",
        "y",
        "yes",
    )
    PROMETHEUS_NODE_INSTANCE_LABEL = os.getenv(
        "PROMETHEUS_NODE_INSTANCE_LABEL", "label_node_kubernetes_io_instance_type"
    )

    # --- OpenCost API URL (used by OpenCostCollector) ---
    OPENCOST_API_URL = os.getenv("OPENCOST_API_URL")
    OPENCOST_VERIFY_CERTS = os.getenv("OPENCOST_VERIFY_CERTS", "True").lower() in (
        "true",
        "1",
        "t",
        "y",
        "yes",
    )

    # --- Default instance profile (used when instance type unknown) ---
    DEFAULT_INSTANCE_VCORES = int(os.getenv("DEFAULT_INSTANCE_VCORES", "1"))
    DEFAULT_INSTANCE_MIN_WATTS = float(os.getenv("DEFAULT_INSTANCE_MIN_WATTS", "1.0"))
    DEFAULT_INSTANCE_MAX_WATTS = float(os.getenv("DEFAULT_INSTANCE_MAX_WATTS", "10.0"))

    # Threshold in cores below which Prometheus totals are considered too small
    LOW_NODE_CPU_THRESHOLD = float(os.getenv("LOW_NODE_CPU_THRESHOLD", "0.05"))

    # Normalization granularity for carbon intensity lookups and cache keys.
    # Allowed values: 'hour', 'day', 'none'
    NORMALIZATION_GRANULARITY = os.getenv("NORMALIZATION_GRANULARITY", "hour").lower()

    # --- Node Analysis variables ---
    NODE_ANALYSIS_INTERVAL = os.getenv("NODE_ANALYSIS_INTERVAL", "5m")
    NODE_DATA_MAX_AGE_DAYS = int(os.getenv("NODE_DATA_MAX_AGE_DAYS", "30"))

    @property
    def DATACENTER_PUE_PROFILES(self):
        return DATACENTER_PUE_PROFILES

    @classmethod
    def validate(cls):
        """
        Validates that the necessary configuration variables are set.
        """
        # Note: Instance variables initialized in __init__ cannot be validated here easily
        # unless we instantiate, but this method is @classmethod.
        # However, we can check os.environ or rely on the fact that config is instantiated below.
        pass

    def validate_instance(self):
        if self.DB_TYPE not in ["sqlite", "postgres", "elasticsearch"]:
            raise ValueError("DB_TYPE must be 'sqlite', 'postgres', or 'elasticsearch'")
        if self.DB_TYPE == "postgres" and not self.DB_CONNECTION_STRING:
            raise ValueError("DB_CONNECTION_STRING must be set for postgres database")
        if self.PROMETHEUS_QUERY_RANGE_STEP:
            match = re.match(r"^(\d+)([smh])$", self.PROMETHEUS_QUERY_RANGE_STEP.lower())
            if not match:
                raise ValueError("PROMETHEUS_QUERY_RANGE_STEP format is invalid. Use 's', 'm', or 'h'.")

            value, unit = int(match.group(1)), match.group(2)
            unit_map = {"s": "seconds", "m": "minutes", "h": "hours"}
            delta = timedelta(**{unit_map[unit]: value})
            step_seconds = delta.total_seconds()

            if (24 * 3600) % step_seconds != 0:
                raise ValueError("PROMETHEUS_QUERY_RANGE_STEP must be a divisor of 24 hours.")
        if not self.ELECTRICITY_MAPS_TOKEN:
            logging.warning("ELECTRICITY_MAPS_TOKEN is not set.")
        if self.NORMALIZATION_GRANULARITY not in ("hour", "day", "none"):
            raise ValueError("NORMALIZATION_GRANULARITY must be one of 'hour', 'day' or 'none'.")


# Instantiate the config to be imported by other modules
config = Config()
config.validate_instance()
