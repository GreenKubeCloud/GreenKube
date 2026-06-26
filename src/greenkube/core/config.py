# src/greenkube/core/config.py

import logging
import os
import re
from datetime import timedelta
from typing import Any, ClassVar, Optional

from pydantic import Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

# Import datacenter PUE profiles
from greenkube.data.datacenter_pue_profiles import DATACENTER_PUE_PROFILES


def _read_secret(key: str, default: str | None = None) -> str | None:
    """
    Read a secret from a mounted file or fall back to an environment variable.

    Resolution order:
    1. ``/etc/greenkube/secrets/<key>`` (Docker secret / K8s volume mount).
    2. Environment variable ``<key>``.
    3. ``default``.

    Raises:
        PermissionError: If the secret file exists but cannot be read due to permissions.
        IOError: If the secret file exists but cannot be read due to I/O errors.
    """
    secret_file = f"/etc/greenkube/secrets/{key}"
    if os.path.exists(secret_file):
        try:
            with open(secret_file, "r") as f:
                value = f.read().strip()
            logging.getLogger(__name__).debug("Loaded secret '%s' from %s", key, secret_file)
            return value
        except PermissionError as e:
            raise PermissionError(
                f"Secret file '{secret_file}' exists but cannot be read due to permission denied. "
                f"Please check file permissions or run with appropriate privileges."
            ) from e
        except (IOError, OSError) as e:
            raise IOError(
                f"Secret file '{secret_file}' exists but cannot be read: {e}. "
                f"Please check the file integrity and system resources."
            ) from e
    return os.getenv(key, default)


class _GreenkubeSecretsSource(PydanticBaseSettingsSource):
    """Custom settings source that reads secrets from /etc/greenkube/secrets/.

    Secret files take priority over environment variables for all fields.
    Files must be named after the field's environment variable name (uppercase).
    Unreadable files raise immediately (fail-fast).
    """

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        # Required by the abstract base; actual loading happens in __call__.
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for field_name, field_info in self.settings_cls.model_fields.items():
            # Use validation_alias (e.g. GREENKUBE_API_KEY) if defined, else the field name.
            alias = field_info.validation_alias
            secret_key = alias if isinstance(alias, str) else field_name
            val = _read_secret(secret_key)
            if val is not None:
                d[field_name] = val
        return d


class Config(BaseSettings):
    """
    Application configuration loaded from environment variables and a .env file.

    All settings are validated at instantiation time (fail-fast). Field names
    match environment variable names exactly (case-sensitive, uppercase).

    Source priority (highest → lowest):
      init kwargs > secrets files (/etc/greenkube/secrets/) > env vars > .env file > defaults
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- Class constant (not loaded from env) ---
    JOULES_PER_KWH: ClassVar[float] = 3.6e6

    # --- Secrets ---
    ELECTRICITY_MAPS_TOKEN: Optional[str] = None
    BOAVIZTA_TOKEN: Optional[str] = None
    ELASTICSEARCH_USER: Optional[str] = None
    ELASTICSEARCH_PASSWORD: Optional[str] = None
    PROMETHEUS_BEARER_TOKEN: Optional[str] = None
    PROMETHEUS_USERNAME: Optional[str] = None
    PROMETHEUS_PASSWORD: Optional[str] = None

    # --- Cluster identification ---
    CLUSTER_NAME: str = ""

    # --- Default variables ---
    DEFAULT_COST: float = 0.0
    DEFAULT_ZONE: str = "unknown"
    DEFAULT_INTENSITY: float = 500
    DEFAULT_HARDWARE_LIFESPAN_YEARS: int = 4
    # Per-instance manufacturing GWP fallback (kg CO₂eq) used when Boavizta does not
    # recognise the cloud provider/instance type. 100 kg is a conservative midpoint.
    DEFAULT_EMBODIED_EMISSIONS_KG: float = 100.0

    # --- Network variables ---
    LOG_LEVEL: str = "INFO"
    # Log output format: "json" (structured, for Loki/Grafana) or "console" (human-readable).
    LOG_FORMAT: str = "json"
    USER_AGENT: str = Field(
        default_factory=lambda: f"GreenKube/{Config._get_version()} (+https://github.com/greenkube)"
    )
    DEFAULT_TIMEOUT_CONNECT: float = 5.0
    DEFAULT_TIMEOUT_READ: float = 15.0

    # --- Database variables ---
    DB_TYPE: str = "postgres"
    DB_PATH: str = "greenkube_data.db"
    DB_CONNECTION_STRING: str = "postgresql://greenkube:greenkube_password@localhost:5432/greenkube"
    DB_SCHEMA: str = "public"
    DB_SSL_MODE: str = "disable"
    DB_POOL_MIN_SIZE: int = 1
    DB_POOL_MAX_SIZE: int = 10
    DB_STATEMENT_TIMEOUT_MS: int = 30000

    # --- Elasticsearch variables ---
    ELASTICSEARCH_HOSTS: str = "http://localhost:9200"
    ELASTICSEARCH_VERIFY_CERTS: bool = True
    ELASTICSEARCH_INDEX_NAME: str = "carbon_intensity"

    # --- Prometheus variables ---
    PROMETHEUS_URL: str = ""
    PROMETHEUS_QUERY_RANGE_STEP: str = "5m"
    PROMETHEUS_QUERY_RANGE_MAX_SAMPLES: int = 10000
    PROMETHEUS_VERIFY_CERTS: bool = True
    PROMETHEUS_NODE_INSTANCE_LABEL: str = "label_node_kubernetes_io_instance_type"

    # --- OpenCost variables ---
    OPENCOST_API_URL: Optional[str] = None
    OPENCOST_VERIFY_CERTS: bool = True

    # --- Boavizta variables ---
    BOAVIZTA_API_URL: str = "https://api.boavizta.org"

    # --- Default instance profile (used when instance type is unknown) ---
    DEFAULT_INSTANCE_VCORES: int = 1
    DEFAULT_INSTANCE_MIN_WATTS: float = 1.0
    DEFAULT_INSTANCE_MAX_WATTS: float = 10.0

    # Threshold in cores below which Prometheus totals are considered too small
    LOW_NODE_CPU_THRESHOLD: float = 0.05

    # Normalization granularity for carbon intensity lookups and cache keys.
    # Allowed values: 'hour', 'day', 'none'
    NORMALIZATION_GRANULARITY: str = "hour"

    # --- Node Analysis variables ---
    NODE_ANALYSIS_INTERVAL: str = "5m"
    NODE_DATA_MAX_AGE_DAYS: int = 30

    # --- Metrics Retention & Compression ---
    # Age threshold (hours) after which raw 5-min metrics are compressed into hourly aggregates.
    METRICS_COMPRESSION_AGE_HOURS: int = 24
    # Maximum number of raw (uncompressed) metrics days to retain.
    METRICS_RAW_RETENTION_DAYS: int = 7
    # Maximum total retention in days for hourly aggregated data. -1 = keep indefinitely.
    METRICS_AGGREGATED_RETENTION_DAYS: int = -1

    # --- Kubernetes client variables ---
    # Timeout (seconds) for individual Kubernetes API calls. 0 = no timeout.
    K8S_REQUEST_TIMEOUT: int = 30

    # --- API variables ---
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    CORS_ORIGINS: str = "*"
    API_KEY: str = Field(default="", validation_alias="GREENKUBE_API_KEY")
    API_RATE_LIMIT: str = "60/minute"
    ROOT_PATH: str = ""
    METRICS_LIST_MAX_RANGE_DAYS: int = 30

    # --- Recommendation Engine variables ---
    RECOMMEND_SYSTEM_NAMESPACES: bool = False
    RECOMMENDATION_LOOKBACK_DAYS: int = 7
    RIGHTSIZING_CPU_THRESHOLD: float = 0.3
    RIGHTSIZING_MEMORY_THRESHOLD: float = 0.3
    RIGHTSIZING_HEADROOM: float = 1.2
    ZOMBIE_COST_THRESHOLD: float = 0.01
    ZOMBIE_ENERGY_THRESHOLD: float = 1000.0
    AUTOSCALING_CV_THRESHOLD: float = 0.7
    AUTOSCALING_SPIKE_RATIO: float = 3.0
    OFF_PEAK_IDLE_THRESHOLD: float = 0.05
    OFF_PEAK_MIN_IDLE_HOURS: int = 4
    IDLE_NAMESPACE_ENERGY_THRESHOLD: float = 1000.0
    CARBON_AWARE_THRESHOLD: float = 1.5
    NODE_UTILIZATION_THRESHOLD: float = 0.2
    # Minimum realistic values — recommendations below these are flagged as unrealistic
    RECOMMENDATION_MIN_CPU_MILLICORES: int = 10
    RECOMMENDATION_MIN_MEMORY_BYTES: int = 16 * 1024 * 1024
    # Tolerance for considering a recommendation "applied" (e.g. 0.25 = 25% deviation allowed)
    RECOMMENDATION_APPLY_TOLERANCE: float = 0.25

    # --- Cloud provider & PUE ---
    # DEFAULT_PUE may be overridden by the datacenter profile for the configured CLOUD_PROVIDER
    # (see _compute_and_validate model validator). The raw env-var value is preserved in
    # _raw_default_pue and used by get_pue_for_provider as the per-node fallback.
    CLOUD_PROVIDER: str = "unknown"
    DEFAULT_PUE: float = 1.3
    # Private: stores the user-configured DEFAULT_PUE before CLOUD_PROVIDER profile override.
    _raw_default_pue: float = PrivateAttr(default=1.3)

    # ------------------------------------------------------------------ #
    # Field validators                                                     #
    # ------------------------------------------------------------------ #

    @field_validator("CLOUD_PROVIDER", mode="after")
    @classmethod
    def _lowercase_cloud_provider(cls, v: str) -> str:
        return v.lower()

    @field_validator("NORMALIZATION_GRANULARITY", mode="after")
    @classmethod
    def _validate_normalization_granularity(cls, v: str) -> str:
        v = v.lower()
        if v not in ("hour", "day", "none"):
            raise ValueError("NORMALIZATION_GRANULARITY must be one of 'hour', 'day' or 'none'.")
        return v

    @field_validator("DB_TYPE", mode="after")
    @classmethod
    def _validate_db_type(cls, v: str) -> str:
        if v not in ["sqlite", "postgres", "elasticsearch"]:
            raise ValueError("DB_TYPE must be 'sqlite', 'postgres', or 'elasticsearch'")
        return v

    @field_validator("PROMETHEUS_QUERY_RANGE_STEP", mode="after")
    @classmethod
    def _validate_prometheus_step(cls, v: str) -> str:
        if v:
            match = re.match(r"^(\d+)([smh])$", v.lower())
            if not match:
                raise ValueError("PROMETHEUS_QUERY_RANGE_STEP format is invalid. Use 's', 'm', or 'h'.")
            value, unit = int(match.group(1)), match.group(2)
            unit_map = {"s": "seconds", "m": "minutes", "h": "hours"}
            delta = timedelta(**{unit_map[unit]: value})
            if (24 * 3600) % delta.total_seconds() != 0:
                raise ValueError("PROMETHEUS_QUERY_RANGE_STEP must be a divisor of 24 hours.")
        return v

    # ------------------------------------------------------------------ #
    # Model validators (cross-field & computed fields)                    #
    # ------------------------------------------------------------------ #

    @model_validator(mode="after")
    def _compute_and_validate(self) -> "Config":
        # Auto-detect cluster name when not explicitly configured
        if not self.CLUSTER_NAME:
            self.CLUSTER_NAME = self._auto_detect_cluster_name()

        # Preserve the raw user-configured DEFAULT_PUE before any profile override.
        # get_pue_for_provider uses this as the per-node fallback so that an unknown
        # node provider receives the explicit human-configured value rather than the
        # cluster-wide CLOUD_PROVIDER's profile PUE.
        object.__setattr__(self, "_raw_default_pue", self.DEFAULT_PUE)

        # Resolve DEFAULT_PUE from the datacenter profile for the configured cloud provider.
        # This overrides any value supplied via the DEFAULT_PUE env var when the provider
        # has a known profile entry.
        profile_key = f"default_{self.CLOUD_PROVIDER}"
        pue = DATACENTER_PUE_PROFILES.get(profile_key)
        if pue is not None:
            self.DEFAULT_PUE = pue
        else:
            logging.getLogger(__name__).warning(
                "Unknown CLOUD_PROVIDER '%s' - falling back to DEFAULT_PUE=%s",
                self.CLOUD_PROVIDER,
                self.DEFAULT_PUE,
            )

        # Cross-field validation
        if self.DB_TYPE == "postgres" and not self.DB_CONNECTION_STRING:
            raise ValueError("DB_CONNECTION_STRING must be set for postgres database")

        if not self.ELECTRICITY_MAPS_TOKEN:
            logging.warning(
                "⚠️  ELECTRICITY_MAPS_TOKEN is not set. CO2 figures will use static fallback data "
                "which may be inaccurate. Get a free token at https://www.electricitymaps.com/"
            )

        return self

    # ------------------------------------------------------------------ #
    # Custom settings sources                                             #
    # ------------------------------------------------------------------ #

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Priority: init kwargs > secret files > env vars > .env file > defaults
        return (init_settings, _GreenkubeSecretsSource(settings_cls), env_settings, dotenv_settings)

    # ------------------------------------------------------------------ #
    # Static helpers                                                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _get_version() -> str:
        """Return the package version string."""
        try:
            from greenkube import __version__

            return __version__
        except Exception:
            return "0.0.0"

    @staticmethod
    def _auto_detect_cluster_name() -> str:
        """Auto-detect the cluster name without requiring extra dependencies.

        Detection order:
        1. ``K8S_NODE_NAME`` env var (injected via the Helm Downward API).
           For single-node clusters (minikube, kind, k3s) the node name equals
           the cluster name, which is the most readable identifier.
        2. Well-known cloud-provider node labels via the Kubernetes Python client
           (only attempted when the ``kubernetes`` package is available).
        3. Hard fallback: ``"default"``.

        Never raises — detection failures are logged at DEBUG level.
        """
        logger = logging.getLogger(__name__)

        # Fast path: Downward API env var injected by the Helm chart.
        node_name = os.getenv("K8S_NODE_NAME", "")
        if node_name:
            logger.debug("Auto-detected cluster name from K8S_NODE_NAME: %s", node_name)
            return node_name

        # Slow path: use the Kubernetes Python client when available.
        try:
            from kubernetes import client as k8s_client  # pyrefly: ignore[missing-import]
            from kubernetes import config as k8s_config  # pyrefly: ignore[missing-import]

            try:
                k8s_config.load_incluster_config()
            except Exception:
                k8s_config.load_kube_config()

            v1 = k8s_client.CoreV1Api()
            nodes = v1.list_node(limit=1, _request_timeout=5)
            if nodes.items:
                node = nodes.items[0]
                labels = node.metadata.labels or {}
                for label in [
                    "alpha.eksctl.io/cluster-name",
                    "eks.amazonaws.com/cluster-name",
                    "cluster.x-k8s.io/cluster-name",
                    "cloud.google.com/gke-cluster-name",
                    "aks.azure.com/cluster-name",
                    "minikube.k8s.io/name",
                ]:
                    if labels.get(label):
                        logger.debug("Auto-detected cluster name from label %s: %s", label, labels[label])
                        return labels[label]
                name = node.metadata.name
                if name:
                    logger.debug("Auto-detected cluster name from first node name: %s", name)
                    return name
        except Exception as exc:
            logger.debug("Could not auto-detect cluster name via k8s client: %s", exc)

        return "default"

    @staticmethod
    def _get_secret(key: str, default: str | None = None) -> str | None:
        """
        Read a secret from a mounted file or fall back to an environment variable.

        Delegates to the module-level :func:`_read_secret` function.

        Raises:
            PermissionError: If the secret file exists but cannot be read due to permissions.
            IOError: If the secret file exists but cannot be read due to I/O errors.
        """
        return _read_secret(key, default)

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    def get_pue_for_provider(self, provider: str | None) -> float:
        """Retrieve the PUE for a specific cloud provider.

        Falls back to the raw user-configured ``DEFAULT_PUE`` when the provider is
        absent or has no entry in DATACENTER_PUE_PROFILES. This is intentionally
        *not* ``self.DEFAULT_PUE`` (which may reflect the cluster CLOUD_PROVIDER
        profile) so that an unknown node provider receives the explicit
        human-configured fallback rather than an unrelated provider's profile value.
        """
        if provider:
            profile_key = f"default_{provider.lower()}"
            pue = DATACENTER_PUE_PROFILES.get(profile_key)
            if pue is not None:
                return pue
        return self._raw_default_pue

    @property
    def DATACENTER_PUE_PROFILES(self) -> dict:
        """Expose the datacenter PUE profiles dict for look-up by callers."""
        return DATACENTER_PUE_PROFILES

    def validate_instance(self) -> None:
        """No-op retained for backward compatibility.

        All validation is now performed automatically by pydantic validators
        during instantiation. Calling this method explicitly is safe but redundant.
        """

    def reload(self) -> None:
        """Re-read all configuration from the current environment variables.

        Useful in tests where ``monkeypatch.setenv`` has been called after the
        singleton was first created. Updates all fields in-place so that
        existing references to the singleton remain valid.
        """
        fresh = self.__class__()
        for field_name in self.__class__.model_fields:
            object.__setattr__(self, field_name, getattr(fresh, field_name))
        # Also sync private attributes (e.g. _raw_default_pue)
        for attr_name in self.__class__.__private_attributes__:
            object.__setattr__(self, attr_name, getattr(fresh, attr_name))


# Module-level singleton – kept for backward compatibility.
# Prefer :func:`get_config` for explicit dependency injection.
config = Config()


def get_config() -> Config:
    """Return the module-level Config singleton.

    Using this function (rather than importing ``config`` directly) makes it
    straightforward to swap or override the instance in tests and enables
    dependency injection throughout the application.
    """
    return config
