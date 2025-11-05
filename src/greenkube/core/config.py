# src/greenkube/core/config.py

import os
import logging
from dotenv import load_dotenv

# Import datacenter PUE profiles
from greenkube.data.datacenter_pue_profiles import DATACENTER_PUE_PROFILES

# Load environment variables from a .env file located in the project root
dotenv_path = os.path.join(os.path.dirname(__file__), '..', '..', '..', '.env')
load_dotenv(dotenv_path=dotenv_path)

class Config:
    """
    Handles the application's configuration by loading values from environment variables.
    """
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
    DEFAULT_ZONE = os.getenv("DEFAULT_ZONE", "FR")
    DEFAULT_INTENSITY = float(os.getenv("DEFAULT_INTENSITY", 0.1))
    JOULES_PER_KWH = 3.6e6

    # --- Logging variables ---
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    # --- Database variables ---
    DB_TYPE = os.getenv("DB_TYPE", "sqlite")
    DB_PATH = os.getenv("DB_PATH", "greenkube_data.db")
    DB_CONNECTION_STRING = os.getenv("DB_CONNECTION_STRING")
    ELECTRICITY_MAPS_TOKEN = os.getenv("ELECTRICITY_MAPS_TOKEN")

    # --- ELASTICSEARCH VARIABLES ---
    ELASTICSEARCH_HOSTS = os.getenv("ELASTICSEARCH_HOSTS", "http://localhost:9200")
    ELASTICSEARCH_USER = os.getenv("ELASTICSEARCH_USER")
    ELASTICSEARCH_PASSWORD = os.getenv("ELASTICSEARCH_PASSWORD")
    ELASTICSEARCH_VERIFY_CERTS = os.getenv("ELASTICSEARCH_VERIFY_CERTS", "True").lower() in ("true", "1", "t", "y", "yes")
    ELASTICSEARCH_INDEX_NAME = os.getenv("ELASTICSEARCH_INDEX_NAME", 'carbon_intensity')

    # -- Prometheus variables ---
    PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus-kube-prometheus-prometheus.monitoring.service.svc.cluster.local:9090")
    PROMETHEUS_QUERY_RANGE_STEP = os.getenv("PROMETHEUS_QUERY_RANGE_STEP", "5m")
    PROMETHEUS_VERIFY_CERTS = os.getenv("PROMETHEUS_VERIFY_CERTS", "True").lower() in ("true", "1", "t", "y", "yes")
    PROMETHEUS_BEARER_TOKEN = os.getenv("PROMETHEUS_BEARER_TOKEN")
    PROMETHEUS_USERNAME = os.getenv("PROMETHEUS_USERNAME")
    PROMETHEUS_PASSWORD = os.getenv("PROMETHEUS_PASSWORD")
    PROMETHEUS_NODE_INSTANCE_LABEL = os.getenv("PROMETHEUS_NODE_INSTANCE_LABEL", "label_node_kubernetes_io_instance_type")

    # --- OpenCost API URL (used by OpenCostCollector) ---
    OPENCOST_API_URL = os.getenv("OPENCOST_API_URL")

    # --- Default instance profile (used when instance type unknown) ---
    DEFAULT_INSTANCE_VCORES = int(os.getenv("DEFAULT_INSTANCE_VCORES", "1"))
    DEFAULT_INSTANCE_MIN_WATTS = float(os.getenv("DEFAULT_INSTANCE_MIN_WATTS", "1.0"))
    DEFAULT_INSTANCE_MAX_WATTS = float(os.getenv("DEFAULT_INSTANCE_MAX_WATTS", "10.0"))

    # Normalization granularity for carbon intensity lookups and cache keys.
    # Allowed values: 'hour', 'day', 'none'
    NORMALIZATION_GRANULARITY = os.getenv("NORMALIZATION_GRANULARITY", "hour").lower()

    @classmethod
    def validate(cls):
        """
        Validates that the necessary configuration variables are set.
        """
        if cls.DB_TYPE not in ["sqlite", "postgres", "elasticsearch"]:
            raise ValueError("DB_TYPE must be 'sqlite', 'postgres', or 'elasticsearch'")
        if cls.DB_TYPE == "postgres" and not cls.DB_CONNECTION_STRING:
            raise ValueError("DB_CONNECTION_STRING must be set for postgres database")
        if not cls.ELECTRICITY_MAPS_TOKEN:
            logging.warning("ELECTRICITY_MAPS_TOKEN is not set.")
        if cls.NORMALIZATION_GRANULARITY not in ("hour", "day", "none"):
            raise ValueError("NORMALIZATION_GRANULARITY must be one of 'hour', 'day' or 'none'.")

# Instantiate the config to be imported by other modules
config = Config()
config.validate()

