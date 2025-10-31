# src/greenkube/core/config.py

import os
import logging
from dotenv import load_dotenv

# Load environment variables from a .env file located in the project root
dotenv_path = os.path.join(os.path.dirname(__file__), '..', '..', '..', '.env')
load_dotenv(dotenv_path=dotenv_path)

class Config:
    """
    Handles the application's configuration by loading values from environment variables.
    """
    # --- Default variables ---
    DEFAULT_COST = 0.0
    DEFAULT_PUE = 1.5
    DEFAULT_ZONE = "FR"
    DEFAULT_INTENSITY = 0.1
    JOULES_PER_KWH = 3.6e6
    GRAMS_PER_KG = 1000

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

    # --- Prometheus connection options ---
    # Whether to verify TLS certificates when connecting to PROMETHEUS_URL
    PROMETHEUS_VERIFY_CERTS = os.getenv("PROMETHEUS_VERIFY_CERTS", "True").lower() in ("true", "1", "t", "y", "yes")
    # Optional bearer token for Prometheus (useful for external or secured endpoints)
    PROMETHEUS_BEARER_TOKEN = os.getenv("PROMETHEUS_BEARER_TOKEN")
    # Optional basic auth credentials
    PROMETHEUS_USERNAME = os.getenv("PROMETHEUS_USERNAME")
    PROMETHEUS_PASSWORD = os.getenv("PROMETHEUS_PASSWORD")

    # Prometheus label for instance type mapping. Some setups use different label keys.
    PROMETHEUS_NODE_INSTANCE_LABEL = os.getenv("PROMETHEUS_NODE_INSTANCE_LABEL", "label_node_kubernetes_io_instance_type")

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

# Instantiate the config to be imported by other modules
config = Config()
config.validate()

