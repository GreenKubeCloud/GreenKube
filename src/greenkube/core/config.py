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

    All values are resolved at instantiation time so that tests can override
    environment variables before creating a new Config and get deterministic
    behaviour. Properties are used only for values that must be dynamically
    derived from other instance attributes.
    """

    # Constants that never change at runtime.
    JOULES_PER_KWH = 3.6e6

    def __init__(self):
        # --- Secrets (files or env vars) ---
        self.ELECTRICITY_MAPS_TOKEN = self._get_secret("ELECTRICITY_MAPS_TOKEN")
        self.BOAVIZTA_TOKEN = self._get_secret("BOAVIZTA_TOKEN")
        self.ELASTICSEARCH_USER = self._get_secret("ELASTICSEARCH_USER")
        self.ELASTICSEARCH_PASSWORD = self._get_secret("ELASTICSEARCH_PASSWORD")
        self.PROMETHEUS_BEARER_TOKEN = self._get_secret("PROMETHEUS_BEARER_TOKEN")
        self.PROMETHEUS_USERNAME = self._get_secret("PROMETHEUS_USERNAME")
        self.PROMETHEUS_PASSWORD = self._get_secret("PROMETHEUS_PASSWORD")

        # --- Default variables ---
        self.DEFAULT_COST = 0.0
        self.DEFAULT_ZONE = os.getenv("DEFAULT_ZONE", "FR")
        self.DEFAULT_INTENSITY = float(os.getenv("DEFAULT_INTENSITY", 500))
        self.DEFAULT_HARDWARE_LIFESPAN_YEARS = int(os.getenv("DEFAULT_HARDWARE_LIFESPAN_YEARS", "4"))

        # --- Network variables ---
        self.LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
        self.USER_AGENT = os.getenv("USER_AGENT", f"GreenKube/{self._get_version()} (+https://github.com/greenkube)")
        self.DEFAULT_TIMEOUT_CONNECT = float(os.getenv("DEFAULT_TIMEOUT_CONNECT", "5.0"))
        self.DEFAULT_TIMEOUT_READ = float(os.getenv("DEFAULT_TIMEOUT_READ", "15.0"))

        # --- Database variables ---
        self.DB_TYPE = os.getenv("DB_TYPE", "postgres")
        self.DB_PATH = os.getenv("DB_PATH", "greenkube_data.db")
        self.DB_CONNECTION_STRING = os.getenv(
            "DB_CONNECTION_STRING", "postgresql://greenkube:greenkube_password@localhost:5432/greenkube"
        )
        self.DB_SCHEMA = os.getenv("DB_SCHEMA", "public")

        # --- Elasticsearch variables ---
        self.ELASTICSEARCH_HOSTS = os.getenv("ELASTICSEARCH_HOSTS", "http://localhost:9200")
        self.ELASTICSEARCH_VERIFY_CERTS = os.getenv("ELASTICSEARCH_VERIFY_CERTS", "True").lower() in (
            "true",
            "1",
            "t",
            "y",
            "yes",
        )
        self.ELASTICSEARCH_INDEX_NAME = os.getenv("ELASTICSEARCH_INDEX_NAME", "carbon_intensity")

        # --- Prometheus variables ---
        self.PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "")
        self.PROMETHEUS_QUERY_RANGE_STEP = os.getenv("PROMETHEUS_QUERY_RANGE_STEP", "5m")
        self.PROMETHEUS_QUERY_RANGE_MAX_SAMPLES = int(os.getenv("PROMETHEUS_QUERY_RANGE_MAX_SAMPLES", "10000"))
        self.PROMETHEUS_VERIFY_CERTS = os.getenv("PROMETHEUS_VERIFY_CERTS", "True").lower() in (
            "true",
            "1",
            "t",
            "y",
            "yes",
        )
        self.PROMETHEUS_NODE_INSTANCE_LABEL = os.getenv(
            "PROMETHEUS_NODE_INSTANCE_LABEL", "label_node_kubernetes_io_instance_type"
        )

        # --- OpenCost variables ---
        self.OPENCOST_API_URL = os.getenv("OPENCOST_API_URL")
        self.OPENCOST_VERIFY_CERTS = os.getenv("OPENCOST_VERIFY_CERTS", "True").lower() in (
            "true",
            "1",
            "t",
            "y",
            "yes",
        )

        # --- Boavizta variables ---
        self.BOAVIZTA_API_URL = os.getenv("BOAVIZTA_API_URL", "https://api.boavizta.org")

        # --- Default instance profile (used when instance type unknown) ---
        self.DEFAULT_INSTANCE_VCORES = int(os.getenv("DEFAULT_INSTANCE_VCORES", "1"))
        self.DEFAULT_INSTANCE_MIN_WATTS = float(os.getenv("DEFAULT_INSTANCE_MIN_WATTS", "1.0"))
        self.DEFAULT_INSTANCE_MAX_WATTS = float(os.getenv("DEFAULT_INSTANCE_MAX_WATTS", "10.0"))

        # Threshold in cores below which Prometheus totals are considered too small
        self.LOW_NODE_CPU_THRESHOLD = float(os.getenv("LOW_NODE_CPU_THRESHOLD", "0.05"))

        # Normalization granularity for carbon intensity lookups and cache keys.
        # Allowed values: 'hour', 'day', 'none'
        self.NORMALIZATION_GRANULARITY = os.getenv("NORMALIZATION_GRANULARITY", "hour").lower()

        # --- Node Analysis variables ---
        self.NODE_ANALYSIS_INTERVAL = os.getenv("NODE_ANALYSIS_INTERVAL", "5m")
        self.NODE_DATA_MAX_AGE_DAYS = int(os.getenv("NODE_DATA_MAX_AGE_DAYS", "30"))

        # --- API variables ---
        self.API_HOST = os.getenv("API_HOST", "0.0.0.0")
        self.API_PORT = int(os.getenv("API_PORT", "8000"))

        # --- Recommendation Engine variables ---
        self.RECOMMEND_SYSTEM_NAMESPACES = os.getenv("RECOMMEND_SYSTEM_NAMESPACES", "false").lower() in (
            "true",
            "1",
            "t",
            "y",
            "yes",
        )
        self.RECOMMENDATION_LOOKBACK_DAYS = int(os.getenv("RECOMMENDATION_LOOKBACK_DAYS", "7"))
        self.RIGHTSIZING_CPU_THRESHOLD = float(os.getenv("RIGHTSIZING_CPU_THRESHOLD", "0.3"))
        self.RIGHTSIZING_MEMORY_THRESHOLD = float(os.getenv("RIGHTSIZING_MEMORY_THRESHOLD", "0.3"))
        self.RIGHTSIZING_HEADROOM = float(os.getenv("RIGHTSIZING_HEADROOM", "1.2"))
        self.ZOMBIE_COST_THRESHOLD = float(os.getenv("ZOMBIE_COST_THRESHOLD", "0.01"))
        self.ZOMBIE_ENERGY_THRESHOLD = float(os.getenv("ZOMBIE_ENERGY_THRESHOLD", "1000"))
        self.AUTOSCALING_CV_THRESHOLD = float(os.getenv("AUTOSCALING_CV_THRESHOLD", "0.7"))
        self.AUTOSCALING_SPIKE_RATIO = float(os.getenv("AUTOSCALING_SPIKE_RATIO", "3.0"))
        self.OFF_PEAK_IDLE_THRESHOLD = float(os.getenv("OFF_PEAK_IDLE_THRESHOLD", "0.05"))
        self.OFF_PEAK_MIN_IDLE_HOURS = int(os.getenv("OFF_PEAK_MIN_IDLE_HOURS", "4"))
        self.IDLE_NAMESPACE_ENERGY_THRESHOLD = float(os.getenv("IDLE_NAMESPACE_ENERGY_THRESHOLD", "1000"))
        self.CARBON_AWARE_THRESHOLD = float(os.getenv("CARBON_AWARE_THRESHOLD", "1.5"))
        self.NODE_UTILIZATION_THRESHOLD = float(os.getenv("NODE_UTILIZATION_THRESHOLD", "0.2"))

    @staticmethod
    def _get_version() -> str:
        """Return the package version string."""
        try:
            from greenkube import __version__

            return __version__
        except Exception:
            return "0.0.0"

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

    @property
    def DATACENTER_PUE_PROFILES(self):
        return DATACENTER_PUE_PROFILES

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

        if not os.getenv("DEFAULT_ZONE"):
            logging.warning(
                f"DEFAULT_ZONE is not set. Using hardcoded default '{self.DEFAULT_ZONE}'. "
                "This may result in inaccurate carbon intensity data if your cluster is not in France."
            )


# Instantiate the config to be imported by other modules
config = Config()
config.validate_instance()
