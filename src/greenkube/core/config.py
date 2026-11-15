# src/greenkube/core/config.py

import logging
import math
import os
import re
from datetime import timedelta
from typing import Any, ClassVar, Optional

from pydantic import AliasChoices, Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

# Import datacenter PUE profiles
from greenkube.data.datacenter_pue_profiles import DATACENTER_PUE_PROFILES

_GITOPS_DURATION_PATTERN = re.compile(r"^(\d+)(ms|s|m|h)$")


def parse_gitops_duration_seconds(value: str) -> float:
    """Parse a short GitOps duration such as ``5m``, ``30s`` or ``2h``."""
    if not isinstance(value, str):
        raise ValueError("GitOps durations must be strings")
    match = _GITOPS_DURATION_PATTERN.fullmatch(value.strip().lower())
    if match is None:
        raise ValueError("GitOps durations must use a number followed by ms, s, m, or h")
    amount = int(match.group(1))
    seconds = amount * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[match.group(2)]
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("GitOps durations must be greater than zero")
    return seconds


#: Allowlist token expanded at run time to the repositories of discovered GitOps sources (F12).
DISCOVERED_REPOSITORIES = "@discovered"


def parse_helm_chart_hosts(value: str) -> tuple[str, ...]:
    """Parse ``GITOPS_HELM_CHART_HOSTS``: a JSON list of host, host:port or *.domain patterns."""
    import json

    text = (value or "").strip()
    if not text:
        return ()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("GITOPS_HELM_CHART_HOSTS must be a JSON list of hosts") from None
    if not isinstance(parsed, list) or any(not isinstance(item, str) or not item.strip() for item in parsed):
        raise ValueError("GITOPS_HELM_CHART_HOSTS must be a JSON list of non-empty hosts")
    hosts = tuple(item.strip().lower() for item in parsed)
    if any(re.fullmatch(r"(?:\*\.)?[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::\d{1,5})?", host) is None for host in hosts):
        raise ValueError("GITOPS_HELM_CHART_HOSTS entries must be host, host:port or *.domain patterns")
    return hosts


def parse_local_cluster_aliases(value: str) -> tuple[str, ...]:
    """Parse ``GITOPS_LOCAL_CLUSTER_ALIASES``: a JSON list of Argo CD server URLs or cluster names.

    They name the cluster GreenKube runs in when Argo CD registers it under another address
    than ``https://kubernetes.default.svc`` or another name than ``in-cluster``.
    """
    import json

    text = (value or "").strip()
    if not text:
        return ()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("GITOPS_LOCAL_CLUSTER_ALIASES must be a JSON list of server URLs or cluster names") from None
    if not isinstance(parsed, list) or any(not isinstance(item, str) or not item.strip() for item in parsed):
        raise ValueError("GITOPS_LOCAL_CLUSTER_ALIASES must be a JSON list of non-empty strings")
    return tuple(item.strip() for item in parsed)


def parse_repository_allowlist(value: str) -> tuple[str, ...]:
    """Parse ``AUTOMATION_REPOSITORY_ALLOWLIST``: a JSON list (or comma list) of patterns."""
    import json

    text = (value or "").strip()
    if not text:
        return (DISCOVERED_REPOSITORIES,)
    if text.startswith("["):
        try:
            items = json.loads(text)
        except json.JSONDecodeError:
            raise ValueError("AUTOMATION_REPOSITORY_ALLOWLIST must be a JSON list of strings") from None
        if not isinstance(items, list):
            raise ValueError("AUTOMATION_REPOSITORY_ALLOWLIST must be a JSON list of strings")
    else:
        items = text.split(",")
    patterns: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise ValueError("AUTOMATION_REPOSITORY_ALLOWLIST must be a JSON list of strings")
        pattern = item.strip()
        if not pattern or any(character.isspace() or ord(character) < 32 for character in pattern):
            raise ValueError("AUTOMATION_REPOSITORY_ALLOWLIST patterns must be non-empty and contain no whitespace")
        if pattern not in patterns:
            patterns.append(pattern)
    if not patterns:
        raise ValueError("AUTOMATION_REPOSITORY_ALLOWLIST needs at least one pattern (use @discovered)")
    return tuple(patterns)


#: Shortest baseline window (days) an accelerated (demo/quickstart) change measurement accepts: 1 hour.
ACCELERATED_MIN_PRE_WINDOW_DAYS = 1 / 24
#: Shortest post-change window (days): a verdict needs 5 complete days of aligned hours (MEA-10).
MIN_POST_WINDOW_DAYS = 5.0
#: Shortest accelerated post-change window (days): six 5-minute slots, 30 minutes (MEA-10).
ACCELERATED_MIN_POST_WINDOW_DAYS = 1 / 48


#: Clamped window settings already reported (name, configured value): warn once per process.
_CLAMP_WARNED: set[tuple[str, float]] = set()


def _clamp_window(name: str, value: float, minimum: float, why: str) -> float:
    """``value`` raised to ``minimum`` with a warning (once per value), never a startup failure."""
    if value >= minimum - 1e-9:
        return value
    if (name, value) not in _CLAMP_WARNED:
        _CLAMP_WARNED.add((name, value))
        logging.getLogger(__name__).warning(
            "%s=%g is below the minimum of %g days (%s): using %g days.", name, value, minimum, why, minimum
        )
    return minimum


def _scan_interval_seconds(value: str) -> int:
    """Parse a scheduler interval such as ``30m`` or ``1h`` (units s, m, h)."""
    match = re.fullmatch(r"(\d+)([smh])", str(value).strip().lower())
    if match is None:
        raise ValueError("RECOMMENDATION_SCAN_INTERVAL must be a number followed by s, m or h (e.g. 1h).")
    return int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)]


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
                # Keyed like the input pydantic validates: an aliased field (for example
                # METRICS_TOKEN <- GREENKUBE_METRICS_TOKEN) ignores its field name.
                d[secret_key] = val
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
    PROMETHEUS_BEARER_TOKEN: Optional[str] = None
    PROMETHEUS_USERNAME: Optional[str] = None
    PROMETHEUS_PASSWORD: Optional[str] = None
    OPENCOST_BEARER_TOKEN: Optional[str] = None
    OPENCOST_USERNAME: Optional[str] = None
    OPENCOST_PASSWORD: Optional[str] = None
    WATTNET_EMAIL: Optional[str] = None
    WATTNET_PASSWORD: Optional[str] = None

    # --- Electricity data provider ---
    # Which data source provides grid carbon intensity: 'electricity_maps' or 'wattnet'.
    ELECTRICITY_PROVIDER: str = "electricity_maps"
    WATTNET_API_BASE_URL: str = "https://api.wattnet.eu/v1"
    WATTNET_TOKEN_SERVICE_URL: str = "https://api.wattnet.eu/token-request"

    # --- Cluster identification ---
    CLUSTER_NAME: str = ""

    # --- Default variables ---
    DEFAULT_COST: float = 0.0
    DEFAULT_ZONE: str = "unknown"
    DEFAULT_INTENSITY: float = 500
    # Maximum age of a stored grid carbon intensity before it is considered
    # stale; beyond it the zone default is used and rows are tagged
    # intensity_source=zone_default (A8). Format: <number><ms|s|m|h>.
    INTENSITY_MAX_AGE: str = "6h"
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
    # Apply pending schema migrations when connecting (F11). The Helm chart sets it
    # to true in the controller only (PostgreSQL advisory lock); the API and the
    # optional worker report not-ready until the schema is current. SQLite always
    # migrates (local development and `greenkube demo`).
    DB_RUN_MIGRATIONS: bool = True

    # --- Prometheus variables ---
    PROMETHEUS_URL: str = ""
    PROMETHEUS_QUERY_RANGE_STEP: str = "5m"
    # Prometheus scrape interval of cAdvisor; rate windows span max(step, 4 x scrape interval).
    PROMETHEUS_SCRAPE_INTERVAL: str = "30s"
    # Collection catch-up: windows end at now - COLLECTION_SAFETY_LAG; after downtime the
    # job backfills missing windows up to COLLECTION_MAX_BACKFILL in the past.
    COLLECTION_SAFETY_LAG: str = "1m"
    COLLECTION_MAX_BACKFILL: str = "24h"
    # Consecutive failed Kubernetes pod/node list attempts for one window before it is
    # stored in degraded mode (rows flagged, reason k8s_list_failed) instead of retried.
    COLLECTION_K8S_LIST_MAX_FAILURES: int = Field(3, ge=1)
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

    # --- Controller observability ---
    # Port of the controller's Prometheus endpoint (/prometheus/metrics and /metrics),
    # bound on all interfaces. A bearer token is required only when the controller
    # has an API key (the Helm chart keeps it out). 0 disables the listener.
    CONTROLLER_METRICS_PORT: int = 9102

    # --- Kubernetes client variables ---
    # Timeout (seconds) for individual Kubernetes API calls. 0 = no timeout.
    K8S_REQUEST_TIMEOUT: int = 30
    # Read per-container CPU/memory from the read-only metrics.k8s.io API.
    KUBERNETES_METRICS_API_ENABLED: bool = True
    LEADER_ELECTION_ENABLED: bool = False
    LEADER_ELECTION_LEASE_NAME: str = Field(
        default="",
        validation_alias=AliasChoices("LEADER_ELECTION_LEASE_NAME", "LEASE_NAME"),
    )
    LEADER_ELECTION_LEASE_NAMESPACE: str = Field(
        default="",
        validation_alias=AliasChoices("LEADER_ELECTION_LEASE_NAMESPACE", "LEASE_NAMESPACE"),
    )

    # --- API variables ---
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    CORS_ORIGINS: str = ""
    TRUSTED_HOSTS: str = "*"
    TRUSTED_PROXY_IPS: str = ""
    ENVIRONMENT: str = "development"
    API_KEY: str = Field(default="", validation_alias="GREENKUBE_API_KEY")
    # Security hardening: what the deprecated bootstrap key GREENKUBE_API_KEY may do:
    # admin (historical; required by API_AUTH_MODE=api_key), metrics (scrape only) or disabled.
    AUTH_LEGACY_API_KEY: str = "admin"
    # Dedicated Prometheus scrape token (Helm: generated by the credentials bootstrap hook);
    # accepted for metrics only, by the API, the controller and the worker.
    METRICS_TOKEN: str = Field(default="", validation_alias="GREENKUBE_METRICS_TOKEN")
    # session (default): browser sign-in with server-side sessions plus Bearer
    # API keys for machines (F14, F15). api_key: Bearer API keys only.
    API_AUTH_MODE: str = "session"
    OIDC_ISSUER: Optional[str] = None
    OIDC_AUDIENCE: Optional[str] = None
    SESSION_COOKIE_NAME: str = "greenkube_session"
    # auto: Secure when the request is HTTPS (directly or via X-Forwarded-Proto
    # from TRUSTED_PROXY_IPS); always; never (local plain-HTTP testing only).
    SESSION_COOKIE_SECURE: str = "auto"
    # Strict-Transport-Security on HTTPS responses (and on every response when
    # SESSION_COOKIE_SECURE=always); 0 disables the header.
    HSTS_MAX_AGE: int = Field(default=31_536_000, ge=0)
    HSTS_INCLUDE_SUBDOMAINS: bool = False
    AUTH_SESSION_IDLE_TIMEOUT: str = "12h"
    AUTH_SESSION_ABSOLUTE_LIFETIME: str = "7d"
    AUTH_SESSION_REMEMBER_LIFETIME: str = "30d"
    AUTH_LOGIN_RATE_LIMIT: str = "5/minute"
    AUTH_ADMIN_USERNAME: str = "admin"
    # Optional bootstrap password for the first admin (Helm auth.admin.existingSecret).
    AUTH_ADMIN_PASSWORD: Optional[str] = None
    # Secret holding the generated initial admin password (Helm: <release>-initial-admin).
    AUTH_INITIAL_ADMIN_SECRET: str = ""
    # --- Single sign-on and roles (F16) ---
    # proxy: identity headers from TRUSTED_PROXY_IPS only; oidc: native OpenID Connect
    # (authorization code + PKCE, OIDC_ISSUER, OIDC_AUDIENCE = client id).
    # Local accounts keep signing in with a password in SSO modes unless this is false.
    AUTH_LOCAL_LOGIN: bool = True
    # Identity headers set (overwritten) by the proxy; the n-th groups header pairs with the n-th
    # user header and only one family may appear on a request. Defaults: oauth2-proxy --pass-user-headers.
    AUTH_PROXY_USER_HEADERS: str = "X-Forwarded-User"
    AUTH_PROXY_GROUPS_HEADERS: str = "X-Forwarded-Groups"
    # JSON object role -> IdP groups, e.g. {"admin": ["platform-admins"], "operator": ["sre"]}.
    AUTH_ROLE_GROUPS: str = ""
    # Role of an SSO user without a mapped group: viewer, operator, admin or none (no access).
    AUTH_DEFAULT_ROLE: str = "viewer"
    OIDC_CLIENT_SECRET: Optional[str] = None
    OIDC_SCOPES: str = "openid profile email"
    OIDC_USERNAME_CLAIM: str = "preferred_username"
    OIDC_GROUPS_CLAIM: str = "groups"
    # Defaults to <GREENKUBE_EXTERNAL_URL>/api/v1/auth/oidc/callback.
    OIDC_REDIRECT_URL: str = ""
    OIDC_PROVIDER_NAME: str = "SSO"
    API_RATE_LIMIT: str = "60/minute"
    ROOT_PATH: str = ""
    METRICS_LIST_MAX_RANGE_DAYS: int = 30

    # --- Recommendation Engine variables ---
    RECOMMEND_SYSTEM_NAMESPACES: bool = False
    # Lookback of recommendation scans; 14 days covers two weekly cycles (E9).
    RECOMMENDATION_LOOKBACK_DAYS: int = 14
    # Hard bound on combined + container metric rows loaded into memory by the
    # row-loading scan path (backends without the workload usage rollup). The
    # lookback window is shortened before materializing a larger result set.
    RECOMMENDATION_MAX_METRIC_ROWS: int = 750_000
    RIGHTSIZING_CPU_THRESHOLD: float = 0.3
    RIGHTSIZING_MEMORY_THRESHOLD: float = 0.3
    RIGHTSIZING_HEADROOM: float = 1.2
    # Two-way rightsizing (E9): recommend a request increase when p99 usage
    # exceeds this share of the request.
    RIGHTSIZING_INCREASE_THRESHOLD: float = 0.9
    # One increase step is capped at this multiple of the current request,
    # unless OOM kills back it; the next scan re-evaluates (USER BUG 2).
    RIGHTSIZING_MAX_INCREASE_FACTOR: float = Field(2.0, gt=1)
    # Minutes after a container (re)start excluded from usage percentiles:
    # startup work (WAL replay, JIT, caches) is not steady-state demand.
    RIGHTSIZING_WARMUP_MINUTES: int = Field(10, ge=0, le=1440)
    # Hours after its last sample a target without a running pod is left out of
    # rightsizing (REC-05): deleted, renamed or scaled-to-zero workloads.
    RECOMMENDATION_TARGET_MAX_STALENESS_HOURS: float = Field(2.0, gt=0)
    # Observed coverage (days) required before a request decrease is recommended.
    RECOMMENDATION_MIN_DECREASE_COVERAGE_DAYS: float = 7.0
    # Limits policy of native rightsizing: keep | remove_cpu | memory_equals_request.
    RECOMMENDATION_LIMITS_POLICY: str = "keep"
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
    # Assumed monthly cost per GiB of provisioned storage (USD), used to estimate
    # the annual cost savings of deleting orphaned PersistentVolumes.
    STORAGE_COST_PER_GIB_MONTH: float = 0.10
    # Assumed monthly cost of a provisioned cloud LoadBalancer (USD), used to
    # estimate the annual cost savings of deleting orphaned LoadBalancer Services.
    LOAD_BALANCER_COST_PER_MONTH: float = 18.0
    # Days a resource must look unused continuously (LoadBalancer, PersistentVolume)
    # or be observed idle (zombie workload, idle namespace) before its deletion is
    # recommended. Never below 0.
    DELETION_MIN_UNUSED_DAYS: float = 7.0

    # --- Impact Model (docs/methodology/impact-model.md) ---
    # CustomRates of the cost fallback chain (observed -> pricing catalog ->
    # custom rates). Unset means the OpenCost default custom pricing
    # (0.031611 per core-hour, 0.004237 per GiB-hour, USD).
    IMPACT_PRICING_CPU_CORE_HOUR: Optional[float] = None
    IMPACT_PRICING_RAM_GIB_HOUR: Optional[float] = None
    IMPACT_PRICING_CURRENCY: str = "USD"
    # Override of the share of released capacity expected to become removed
    # billable node-hours. Unset (default) lets RealizationContext (roadmap B6)
    # derive it from the cluster's autoscalers, consolidation policies and
    # disruption blockers; a value pins it for every projection.
    IMPACT_REALIZATION_FACTOR: Optional[float] = None
    # Autoscaler of the nodes outside Karpenter pools when detection cannot see it
    # (IMP-12): none | cluster_autoscaler | karpenter. Empty: detected from the
    # Cluster Autoscaler status ConfigMap and Karpenter NodePools.
    IMPACT_AUTOSCALER_HINT: str = ""
    # Memory power per GiB of node memory (MemoryPowerModel, roadmap B5). The
    # default is the Cloud Carbon Footprint coefficient (0.392 W/GB).
    POWER_MEMORY_W_PER_GIB: float = 0.392
    # Carbon price (currency per tonne CO2e) that converts projected CO2e into
    # value: value_annual = cost + CARBON_PRICE_PER_TONNE * tCO2e (roadmap I2, D-2).
    CARBON_PRICE_PER_TONNE: float = 100.0

    # --- Recommendation sources (connectors) ---
    # VPA recommendation mode (updateMode: Off) is used for CPU/memory rightsizing
    # when enabled; native rightsizing for the same workload is then suppressed.
    # Phase 6 enables the source by default; it degrades gracefully when the VPA
    # CRD is not installed.
    RECOMMENDATION_VPA_ENABLED: bool = True
    # Karpenter NodePool consolidation recommendations. Enabled by default and
    # skipped gracefully when the Karpenter CRDs are absent.
    RECOMMENDATION_KARPENTER_ENABLED: bool = True
    # Source precedence for capability arbitration, highest priority first.
    RECOMMENDATION_SOURCE_PRIORITY: str = "vpa,karpenter,greenkube"
    # How the proposals of several sources on one target are combined (roadmap
    # E6): safest (least aggressive), median, weighted (track-record weighted
    # median) or priority (RECOMMENDATION_SOURCE_PRIORITY wins).
    RECOMMENDATION_CONSENSUS_POLICY: str = "safest"
    # Source SDK (roadmap E5, docs/sources.md). Load third-party sources
    # registered under the `greenkube.sources` Python entry points.
    SOURCES_PLUGINS_ENABLED: bool = True
    # Per-source settings as a JSON object keyed by source id, e.g.
    # {"vpa": {"enabled": "auto"}, "greenkube.carbon_aware": {"enabled": false}}.
    # "enabled" is true, false or auto and overrides the RECOMMENDATION_*_ENABLED flags.
    SOURCES_CONFIG: str = ""

    # --- Recommendation ranking & review ---
    # Time-to-live of a recommendation before it is considered expired.
    RECOMMENDATION_TTL_DAYS: int = 14
    # Ranking profile: balanced, carbon_first, cost_first, quick_wins, low_risk.
    # Profiles are presets over the carbon price, risk aversion and effort
    # weight of the expected value (EV) ranking (roadmap I1/I2).
    RECOMMENDATION_RANKING_PROFILE: str = "balanced"
    # Deprecated and ignored since the EV ranking (I1): weights of the former
    # batch-normalized score. Kept so existing values files still load.
    RECOMMENDATION_RANKING_WEIGHTS: str = ""
    # Expected cost (currency) of a failed high-risk change on one workload of
    # normal criticality, scaled by risk level, blast radius and profile (I1).
    RECOMMENDATION_INCIDENT_COST: float = 100.0
    # Cost (currency) of one engineer hour. 0 ignores implementation effort
    # except in the quick_wins profile (I1).
    RECOMMENDATION_ENGINEER_HOUR_COST: float = 0.0
    # Weight of the allocated cost a change releases beyond its billable projection
    # (allocated - expected billable) in the ranking value (IMP-03). On fixed nodes the
    # billable projection is 0, so 1.0 ranks by the capacity freed for other workloads;
    # 0 ranks by the billable projection only.
    RECOMMENDATION_CAPACITY_VALUE_WEIGHT: float = 1.0
    # Minimum number of raw samples expected for a high-confidence recommendation.
    RECOMMENDATION_MIN_SAMPLES: int = 36

    # --- Recommendation verification (Phase 3) ---
    # Observation window after apply before the outcome is verified.
    VERIFICATION_WINDOW_HOURS: float = 72
    # Minimum raw samples required before a verdict is rendered.
    VERIFICATION_MIN_SAMPLES: int = 36
    # Measured savings must reach this ratio of the projection to pass the cost gate.
    VERIFICATION_MIN_SAVINGS_RATIO: float = 0.5
    # p95 usage must stay below proposed request x this factor to pass the health gate.
    VERIFICATION_USAGE_HEADROOM: float = 1.1
    # Maximum allowed restart-count increase during the observation window.
    VERIFICATION_MAX_RESTART_DELTA: int = 0
    # Minimum readiness ratio during the observation window.
    VERIFICATION_MIN_READINESS: float = 0.99
    # Maximum allowed CPU throttling ratio during the observation window.
    VERIFICATION_MAX_THROTTLE_RATIO: float = 0.05
    # --- Change measurement (roadmap C1-C5, docs/methodology/savings-measurement.md) ---
    # Record every detected change with a baseline snapshot and measure it with a
    # counterfactual and a bootstrap CI. Off: changes are measured by the interim verifier.
    CHANGE_MEASUREMENT_ENABLED: bool = True
    # Baseline window (days) ending at the PR merge or plan approval; at least 7 so every hour of the week matches
    # (at least 1 hour with CHANGE_MEASUREMENT_ACCELERATED, see below).
    CHANGE_MEASUREMENT_PRE_WINDOW_DAYS: float = Field(7.0, gt=0)
    # Post-change window (days) starting after the rollout and its warm-up; at least 5 (a verdict needs
    # 5 days of aligned hours), at least 30 minutes with CHANGE_MEASUREMENT_ACCELERATED.
    CHANGE_MEASUREMENT_POST_WINDOW_DAYS: float = Field(7.0, gt=0)
    # Hours after rollout completion excluded from the post-window.
    CHANGE_MEASUREMENT_WARMUP_HOURS: float = Field(1.0, ge=0)
    # Minimum share of post-window hours matched with a baseline hour of the week.
    CHANGE_MEASUREMENT_MIN_COVERAGE: float = Field(0.8, gt=0, le=1)
    # Circular block bootstrap resamples for the within-window part of the 90% confidence interval.
    CHANGE_MEASUREMENT_BOOTSTRAP_SAMPLES: int = Field(1000, ge=100, le=100000)
    # Noise floor: realized only when the CI lower bound is above this share of the counterfactual,
    # not realized when the CI upper bound is at most this share.
    CHANGE_MEASUREMENT_EPSILON_RATIO: float = Field(0.02, ge=0, le=1)
    # Drift monitor (C8): a realized change regresses when at least this share of its per-replica
    # saving is lost on each of CHANGE_DRIFT_SUSTAINED_DAYS consecutive days.
    CHANGE_DRIFT_THRESHOLD: float = Field(0.5, gt=0, le=1)
    CHANGE_DRIFT_SUSTAINED_DAYS: int = Field(3, ge=1, le=7)
    # Demo and quickstart only: lifts the 7-day minimum of the baseline window down to 1 hour so a
    # change is decided minutes after it lands. Off in every production default.
    CHANGE_MEASUREMENT_ACCELERATED: bool = False
    # Week-to-week coefficient of variation of a measured quantity assumed when the baseline holds
    # fewer than two complete weeks (MEA-01); with two or more it is estimated from the baseline.
    CHANGE_MEASUREMENT_PRIOR_WEEK_CV: float = Field(0.05, ge=0, le=1)
    # --- Savings ledger v2 (roadmap C6) ---
    # How often the controller catches the hash-chained ledger up and verifies its chain (s, m or h).
    LEDGER_CHAIN_VERIFY_INTERVAL: str = "1h"
    # Interval between lifecycle jobs (apply detection, verification, expiry).
    RECOMMENDATION_LIFECYCLE_INTERVAL: str = "5m"
    # Interval between scheduled, leader-gated recommendation scans in the
    # controller (E1). API requests only enqueue runs. Accepts s, m or h.
    RECOMMENDATION_SCAN_INTERVAL: str = "1h"

    # --- Always-on automation (F12) ---
    # Change freeze and incident switch: plan approval returns a policy error and no
    # pull request is opened; discovery, previews and measurement keep running.
    AUTOMATION_PAUSE_DELIVERY: bool = False
    # JSON list of repositories automation may open pull requests against, for
    # inferred providers and providers without their own allowlist. "@discovered":
    # repositories referenced by discovered Argo CD/Flux sources.
    AUTOMATION_REPOSITORY_ALLOWLIST: str = '["@discovered"]'
    # Secret (in the release namespace, mounted under GITOPS_SECRET_MOUNT) read for
    # inferred providers: key "token", or "appId" + "privateKey" for a GitHub App.
    GITOPS_DEFAULT_CREDENTIALS_SECRET: str = "greenkube-git-credentials"
    # How approved plans are delivered (F4): "direct" (GreenKube opens the pull
    # request), "patch" (an operator downloads the patch and opens it) or "ci" (a CI
    # job applies the patch with its own token and reports the pull request back).
    # patch and ci never need write credentials in the cluster.
    AUTOMATION_DELIVERY_MODE: str = "direct"
    # Policy-based auto-approval (F5), opt-in: a preview whose recommendations all
    # meet these limits is approved without the extra click (still a reviewed PR).
    # Contested consensus is never auto-approved, and a decrease needs native
    # evidence or at least two independent consensus votes.
    AUTOMATION_AUTO_APPROVE_ENABLED: bool = False
    AUTOMATION_AUTO_APPROVE_MAX_RISK: str = "low"
    AUTOMATION_AUTO_APPROVE_MIN_CONFIDENCE: float = Field(0.8, ge=0.0, le=1.0)
    AUTOMATION_AUTO_APPROVE_CONTESTED: bool = False
    # Container resources v2 (H1): a request increase (reliability fix, costs money)
    # is auto-approved only when this is true; off by default.
    AUTOMATION_AUTO_APPROVE_INCREASES: bool = False
    # Deprecated (0.3.x, removed in 0.5): automation is always on. An explicit
    # AUTOMATION_ENABLED=false or AUTOMATION_MODE=disabled pauses delivery.
    AUTOMATION_ENABLED: Optional[bool] = None
    AUTOMATION_MODE: Optional[str] = None

    # --- GitOps discovery ---
    # Duration strings (ms, s, m, h). Discovery defaults to five minutes and
    # bindings remain fresh for thirty minutes, matching the operator spec.
    GITOPS_DISCOVERY_INTERVAL: str = "5m"
    GITOPS_BINDING_TTL: str = "30m"
    # Delivery safety: JSON list of extra Argo CD destination server URLs or cluster names
    # that address this cluster. Applications deploying elsewhere never bind a local
    # workload or resource (destination_remote).
    GITOPS_LOCAL_CLUSTER_ALIASES: str = "[]"

    # --- Pull-request experience (roadmap J1-J5, docs/automation.md) ---
    # Public URL of the GreenKube UI, for deep links in PRs (/recommendations/{id},
    # /changes/{plan_id}, /ledger?change=). Empty: PRs carry no links.
    GREENKUBE_EXTERNAL_URL: str = ""
    # Prefix of PR titles ("greenkube: rightsize payments/api ..."); empty drops it.
    GITOPS_PR_TITLE_PREFIX: str = "greenkube"
    # PR body template with {{ section }} placeholders; empty uses the built-in template.
    GITOPS_PR_TEMPLATE: str = ""
    # Add greenkube, optimization/<capability>, risk/<level> and reliability labels.
    GITOPS_PR_DEFAULT_LABELS: bool = True
    # Open high-risk plans as draft PRs (GitLab "Draft:", Gitea "WIP:").
    GITOPS_PR_DRAFT_HIGH_RISK: bool = True
    # Request reviews from the CODEOWNERS of the touched paths.
    GITOPS_PR_CODEOWNERS: bool = True
    # Publish the greenkube/validation commit status on the PR head.
    GITOPS_PR_COMMIT_STATUS: bool = True
    # Keep one GreenKube status comment up to date on merged PRs (synced, applied, measuring, outcome).
    GITOPS_PR_LIFECYCLE_COMMENTS: bool = True
    # How long after the merge the status comment keeps being refreshed.
    GITOPS_PR_LIFECYCLE_LOOKBACK_DAYS: int = Field(30, ge=1, le=365)

    # --- Helm remote charts (H2, docs/automation.md#helm-support) ---
    # The controller pre-fetches the exact remote chart (HTTP repository or OCI registry)
    # a discovered Argo CD/Flux source deployed, verifies its digest and renders it offline
    # in the renderer sandbox. false keeps remote charts fail-closed (remote-charts-disabled).
    GITOPS_HELM_REMOTE_CHARTS: bool = True
    # JSON list of chart hosts the controller may contact ("host", "host:port", "*.domain").
    # Empty: any public host named by a discovered chart source. Loopback, link-local and
    # metadata addresses are always refused; plain-HTTP hosts must be listed.
    GITOPS_HELM_CHART_HOSTS: str = "[]"
    # JSON list of per-chart values-path mappings, checked before the built-in registry:
    # [{"chart": "nginx", "version": "18.3.1", "container": "nginx", "path": "resources"}].
    GITOPS_HELM_CHART_MAPPINGS: str = "[]"

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

    @field_validator("ELECTRICITY_PROVIDER", mode="after")
    @classmethod
    def _validate_electricity_provider(cls, v: str) -> str:
        v = v.lower()
        if v not in ("electricity_maps", "wattnet"):
            raise ValueError("ELECTRICITY_PROVIDER must be 'electricity_maps' or 'wattnet'.")
        return v

    @field_validator("DB_TYPE", mode="after")
    @classmethod
    def _validate_db_type(cls, v: str) -> str:
        if v not in ["sqlite", "postgres"]:
            raise ValueError("DB_TYPE must be 'sqlite' or 'postgres'")
        return v

    @field_validator("AUTOMATION_MODE", mode="after")
    @classmethod
    def _validate_automation_mode(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        mode = value.strip().lower()
        if mode not in {"disabled", "inline", "worker"}:
            raise ValueError("AUTOMATION_MODE must be 'disabled', 'inline', or 'worker'.")
        return mode

    @field_validator("AUTOMATION_DELIVERY_MODE", mode="after")
    @classmethod
    def _validate_delivery_mode(cls, value: str) -> str:
        mode = (value or "").strip().lower() or "direct"
        if mode not in {"direct", "patch", "ci"}:
            raise ValueError("AUTOMATION_DELIVERY_MODE must be 'direct', 'patch' or 'ci'.")
        return mode

    @field_validator("AUTOMATION_AUTO_APPROVE_MAX_RISK", mode="after")
    @classmethod
    def _validate_auto_approve_max_risk(cls, value: str) -> str:
        risk = (value or "").strip().lower()
        if risk not in {"low", "medium", "high"}:
            raise ValueError("AUTOMATION_AUTO_APPROVE_MAX_RISK must be 'low', 'medium' or 'high'.")
        return risk

    @field_validator("AUTOMATION_AUTO_APPROVE_CONTESTED", mode="after")
    @classmethod
    def _validate_auto_approve_contested(cls, value: bool) -> bool:
        if value:
            # Contested consensus is a hard stop (E6, F5): sources disagree, a human decides.
            raise ValueError(
                "AUTOMATION_AUTO_APPROVE_CONTESTED cannot be true: contested advice is never auto-approved."
            )
        return False

    @field_validator("AUTOMATION_REPOSITORY_ALLOWLIST", mode="after")
    @classmethod
    def _validate_repository_allowlist(cls, value: str) -> str:
        parse_repository_allowlist(value)
        return value

    @field_validator("GREENKUBE_EXTERNAL_URL", mode="after")
    @classmethod
    def _validate_external_url(cls, value: str) -> str:
        text = value.strip().rstrip("/")
        if text and (
            not text.startswith(("https://", "http://"))
            or any(character.isspace() for character in text)
            or "?" in text
            or "#" in text
        ):
            raise ValueError("GREENKUBE_EXTERNAL_URL must be an http(s) URL without query or fragment.")
        return text

    @field_validator("GITOPS_PR_TITLE_PREFIX", mode="after")
    @classmethod
    def _validate_pr_title_prefix(cls, value: str) -> str:
        text = value.strip()
        if len(text) > 32 or any(ord(character) < 32 for character in text):
            raise ValueError("GITOPS_PR_TITLE_PREFIX must be at most 32 printable characters.")
        return text

    @field_validator("GITOPS_PR_TEMPLATE", mode="after")
    @classmethod
    def _validate_pr_template(cls, value: str) -> str:
        if not value.strip():
            return ""
        from greenkube.automation.pr_content.template import validate_template

        return validate_template(value)

    @field_validator("GITOPS_LOCAL_CLUSTER_ALIASES", mode="after")
    @classmethod
    def _validate_local_cluster_aliases(cls, value: str) -> str:
        parse_local_cluster_aliases(value)
        return value

    @field_validator("GITOPS_HELM_CHART_HOSTS", mode="after")
    @classmethod
    def _validate_helm_chart_hosts(cls, value: str) -> str:
        parse_helm_chart_hosts(value)
        return value

    @field_validator("GITOPS_HELM_CHART_MAPPINGS", mode="after")
    @classmethod
    def _validate_helm_chart_mappings(cls, value: str) -> str:
        from greenkube.gitops.planning.helm_mappings import ChartMappingConfigError, parse_chart_mappings

        try:
            parse_chart_mappings(value)
        except ChartMappingConfigError as exc:
            raise ValueError(f"GITOPS_HELM_CHART_MAPPINGS is invalid: {exc}") from None
        return value

    @field_validator("GITOPS_DISCOVERY_INTERVAL", mode="before")
    @classmethod
    def _default_blank_discovery_interval(cls, value: Any) -> Any:
        return "5m" if value == "" else value

    @field_validator("GITOPS_BINDING_TTL", mode="before")
    @classmethod
    def _default_blank_binding_ttl(cls, value: Any) -> Any:
        return "30m" if value == "" else value

    @field_validator("GITOPS_DISCOVERY_INTERVAL", mode="after")
    @classmethod
    def _validate_gitops_discovery_interval(cls, value: str) -> str:
        seconds = parse_gitops_duration_seconds(value)
        if not 1 <= seconds <= 3600:
            raise ValueError("GITOPS_DISCOVERY_INTERVAL must be between 1 second and 1 hour")
        return value

    @field_validator("GITOPS_BINDING_TTL", mode="after")
    @classmethod
    def _validate_gitops_binding_ttl(cls, value: str) -> str:
        seconds = parse_gitops_duration_seconds(value)
        if not 1 <= seconds <= 86_400:
            raise ValueError("GITOPS_BINDING_TTL must be between 1 second and 24 hours")
        return value

    @field_validator("INTENSITY_MAX_AGE", mode="before")
    @classmethod
    def _default_blank_intensity_max_age(cls, value: Any) -> Any:
        return "6h" if value == "" or value is None else value

    @field_validator("INTENSITY_MAX_AGE", mode="after")
    @classmethod
    def _validate_intensity_max_age(cls, value: str) -> str:
        try:
            parse_gitops_duration_seconds(value)
        except ValueError as exc:
            raise ValueError("INTENSITY_MAX_AGE must be a duration such as '6h', '90m' or '3600s'") from exc
        return value.strip().lower()

    @field_validator("CONTROLLER_METRICS_PORT", mode="after")
    @classmethod
    def _validate_controller_metrics_port(cls, value: int) -> int:
        if not 0 <= value <= 65_535:
            raise ValueError("CONTROLLER_METRICS_PORT must be between 0 (disabled) and 65535")
        return value

    @field_validator("RECOMMENDATION_LIMITS_POLICY", mode="after")
    @classmethod
    def _validate_recommendation_limits_policy(cls, value: str) -> str:
        normalized = (value or "keep").strip().lower()
        if normalized not in {"keep", "remove_cpu", "memory_equals_request"}:
            raise ValueError("RECOMMENDATION_LIMITS_POLICY must be keep, remove_cpu or memory_equals_request")
        return normalized

    @field_validator("RECOMMENDATION_CONSENSUS_POLICY", mode="after")
    @classmethod
    def _validate_recommendation_consensus_policy(cls, value: str) -> str:
        from greenkube.signals.consensus import parse_policy

        return parse_policy(value).value

    @field_validator("RECOMMENDATION_MAX_METRIC_ROWS", mode="after")
    @classmethod
    def _validate_recommendation_max_metric_rows(cls, value: int) -> int:
        if value < 10_000:
            raise ValueError("RECOMMENDATION_MAX_METRIC_ROWS must be at least 10000")
        return value

    @field_validator("SOURCES_CONFIG", mode="after")
    @classmethod
    def _validate_sources_config(cls, value: str) -> str:
        from greenkube.signals.registry import parse_sources_config

        try:
            parse_sources_config(value.strip())
        except ValueError as exc:
            raise ValueError(f"SOURCES_CONFIG must be a JSON object keyed by source id: {exc}") from exc
        return value.strip()

    @field_validator("RECOMMENDATION_SCAN_INTERVAL", mode="before")
    @classmethod
    def _default_blank_recommendation_scan_interval(cls, value: Any) -> Any:
        return "1h" if value in ("", None) else value

    @field_validator("RECOMMENDATION_SCAN_INTERVAL", mode="after")
    @classmethod
    def _validate_recommendation_scan_interval(cls, value: str) -> str:
        normalized = value.strip().lower()
        seconds = _scan_interval_seconds(normalized)
        if not 60 <= seconds <= 86_400:
            raise ValueError("RECOMMENDATION_SCAN_INTERVAL must be between 1m and 24h.")
        return normalized

    @field_validator("LEDGER_CHAIN_VERIFY_INTERVAL", mode="after")
    @classmethod
    def _validate_ledger_chain_verify_interval(cls, value: str) -> str:
        normalized = str(value).strip().lower()
        match = re.fullmatch(r"(\d+)([smh])", normalized)
        seconds = int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)] if match else 0
        if not 60 <= seconds <= 86_400:
            raise ValueError("LEDGER_CHAIN_VERIFY_INTERVAL must be a duration between 1m and 24h (e.g. 1h).")
        return normalized

    @field_validator("RECOMMENDATION_APPLY_TOLERANCE", mode="after")
    @classmethod
    def _validate_recommendation_apply_tolerance(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("RECOMMENDATION_APPLY_TOLERANCE must be between 0 and 1.")
        return value

    @field_validator("IMPACT_PRICING_CPU_CORE_HOUR", "IMPACT_PRICING_RAM_GIB_HOUR", mode="before")
    @classmethod
    def _blank_impact_price_is_unset(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("IMPACT_PRICING_CPU_CORE_HOUR", "IMPACT_PRICING_RAM_GIB_HOUR", mode="after")
    @classmethod
    def _validate_impact_price(cls, value: Optional[float]) -> Optional[float]:
        if value is not None and value < 0:
            raise ValueError("IMPACT_PRICING_* rates must not be negative.")
        return value

    @field_validator("IMPACT_PRICING_CURRENCY", mode="after")
    @classmethod
    def _validate_impact_currency(cls, value: str) -> str:
        currency = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("IMPACT_PRICING_CURRENCY must be a three-letter currency code (e.g. USD, EUR).")
        return currency

    @field_validator("IMPACT_AUTOSCALER_HINT", mode="after")
    @classmethod
    def _validate_autoscaler_hint(cls, value: str) -> str:
        hint = (value or "").strip().lower()
        if hint and hint not in ("none", "cluster_autoscaler", "karpenter"):
            raise ValueError("IMPACT_AUTOSCALER_HINT must be empty, none, cluster_autoscaler or karpenter.")
        return hint

    @field_validator("IMPACT_REALIZATION_FACTOR", mode="after")
    @classmethod
    def _validate_realization_factor(cls, value: Optional[float]) -> Optional[float]:
        if value is not None and not 0 <= value <= 1:
            raise ValueError("IMPACT_REALIZATION_FACTOR must be between 0 and 1.")
        return value

    @field_validator("POWER_MEMORY_W_PER_GIB", mode="after")
    @classmethod
    def _validate_memory_power(cls, value: float) -> float:
        if value < 0:
            raise ValueError("POWER_MEMORY_W_PER_GIB must not be negative.")
        return value

    @field_validator("CARBON_PRICE_PER_TONNE", mode="after")
    @classmethod
    def _validate_carbon_price(cls, value: float) -> float:
        if value < 0:
            raise ValueError("CARBON_PRICE_PER_TONNE must not be negative.")
        return value

    @field_validator(
        "RECOMMENDATION_INCIDENT_COST",
        "RECOMMENDATION_ENGINEER_HOUR_COST",
        "RECOMMENDATION_CAPACITY_VALUE_WEIGHT",
        mode="after",
    )
    @classmethod
    def _validate_decision_costs(cls, value: float, info) -> float:
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{info.field_name} must be a finite, non-negative amount.")
        return value

    @field_validator("PROMETHEUS_QUERY_RANGE_STEP", mode="after")
    @classmethod
    def _validate_prometheus_step(cls, v: str) -> str:
        if v:
            match = re.match(r"^(\d+)([smh])$", v.lower())
            if not match:
                raise ValueError("PROMETHEUS_QUERY_RANGE_STEP format is invalid. Use 's', 'm', or 'h'.")
            value, unit = int(match.group(1)), match.group(2)
            if value <= 0:
                raise ValueError("PROMETHEUS_QUERY_RANGE_STEP must be greater than zero.")
            unit_map = {"s": "seconds", "m": "minutes", "h": "hours"}
            delta = timedelta(**{unit_map[unit]: value})
            total_seconds = delta.total_seconds()
            if total_seconds <= 0 or (24 * 3600) % total_seconds != 0:
                raise ValueError("PROMETHEUS_QUERY_RANGE_STEP must be a divisor of 24 hours.")
        return v

    @field_validator("SESSION_COOKIE_SECURE", mode="before")
    @classmethod
    def _validate_session_cookie_secure(cls, value: Any) -> str:
        normalized = str(value).strip().lower() if value is not None else "auto"
        aliases = {"true": "always", "1": "always", "yes": "always", "false": "never", "0": "never", "no": "never"}
        normalized = aliases.get(normalized, normalized or "auto")
        if normalized not in ("auto", "always", "never"):
            raise ValueError("SESSION_COOKIE_SECURE must be 'auto', 'always' or 'never'.")
        return normalized

    @field_validator(
        "AUTH_SESSION_IDLE_TIMEOUT", "AUTH_SESSION_ABSOLUTE_LIFETIME", "AUTH_SESSION_REMEMBER_LIFETIME", mode="after"
    )
    @classmethod
    def _validate_session_durations(cls, value: str, info) -> str:
        text = str(value).strip().lower()
        if not re.match(r"^\d+[smhd]$", text) or int(text[:-1]) <= 0:
            raise ValueError(f"{info.field_name} must be a positive duration such as '30m', '12h' or '7d'.")
        return text

    @field_validator("AUTH_LOGIN_RATE_LIMIT", mode="after")
    @classmethod
    def _validate_login_rate_limit(cls, value: str) -> str:
        from limits import parse_many

        text = str(value).strip()
        try:
            parsed = parse_many(text) if "/" in text else []
        except ValueError:
            parsed = []
        if len(parsed) != 1 or parsed[0].amount <= 0:
            raise ValueError("AUTH_LOGIN_RATE_LIMIT must be a single rate such as '5/minute' or '10/hour'.")
        return text

    @field_validator("PROMETHEUS_SCRAPE_INTERVAL", "COLLECTION_SAFETY_LAG", "COLLECTION_MAX_BACKFILL", mode="after")
    @classmethod
    def _validate_collection_durations(cls, value: str, info) -> str:
        if not re.match(r"^\d+[smhd]$", str(value).strip().lower()) or int(str(value).strip()[:-1]) <= 0:
            raise ValueError(f"{info.field_name} must be a positive duration such as '30s', '5m', '1h' or '1d'.")
        return str(value).strip().lower()

    # ------------------------------------------------------------------ #
    # Model validators (cross-field & computed fields)                    #
    # ------------------------------------------------------------------ #

    @model_validator(mode="after")
    def _compute_and_validate(self) -> "Config":
        self.ENVIRONMENT = self.ENVIRONMENT.lower()
        self.API_AUTH_MODE = self.API_AUTH_MODE.lower()
        if self.ENVIRONMENT not in ("development", "test", "production"):
            raise ValueError("ENVIRONMENT must be 'development', 'test', or 'production'.")
        if self.API_AUTH_MODE not in ("api_key", "oidc", "proxy", "session"):
            raise ValueError("API_AUTH_MODE must be 'api_key', 'oidc', 'proxy', or 'session'.")
        if self.API_AUTH_MODE == "oidc" and (not self.OIDC_ISSUER or not self.OIDC_AUDIENCE):
            raise ValueError("OIDC_ISSUER and OIDC_AUDIENCE are required when API_AUTH_MODE is 'oidc'.")
        if self.API_AUTH_MODE == "oidc" and not str(self.OIDC_ISSUER).startswith("https://"):
            if self.ENVIRONMENT == "production" or not str(self.OIDC_ISSUER).startswith("http://"):
                raise ValueError("OIDC_ISSUER must be an https:// URL.")
        self.AUTH_LEGACY_API_KEY = str(self.AUTH_LEGACY_API_KEY or "admin").strip().lower()
        if self.AUTH_LEGACY_API_KEY not in ("admin", "metrics", "disabled"):
            raise ValueError("AUTH_LEGACY_API_KEY must be 'admin', 'metrics' or 'disabled'.")
        if self.API_AUTH_MODE == "api_key" and self.AUTH_LEGACY_API_KEY != "admin":
            # Fail closed and explain: api_key mode has no sign-in, so nobody could administer it.
            raise ValueError(
                "API_AUTH_MODE 'api_key' needs AUTH_LEGACY_API_KEY=admin: the bootstrap key is its only "
                "administrator. Switch to API_AUTH_MODE=session to restrict or disable the bootstrap key."
            )
        if self.API_AUTH_MODE == "proxy" and not self.TRUSTED_PROXY_IPS.strip():
            # Fail closed: identity headers from anyone would let any client choose its identity.
            raise ValueError("TRUSTED_PROXY_IPS must list the authentication proxy when API_AUTH_MODE is 'proxy'.")
        _user_headers = [h for h in str(self.AUTH_PROXY_USER_HEADERS or "").split(",") if h.strip()]
        _group_headers = [h for h in str(self.AUTH_PROXY_GROUPS_HEADERS or "").split(",") if h.strip()]
        if len(_group_headers) > max(1, len(_user_headers)):
            raise ValueError("AUTH_PROXY_GROUPS_HEADERS must not list more headers than AUTH_PROXY_USER_HEADERS.")
        if self.API_AUTH_MODE != "api_key" and not self.SESSION_COOKIE_NAME:
            raise ValueError("SESSION_COOKIE_NAME must not be empty when browser sessions are enabled.")
        from greenkube.auth.roles import normalize_default_role, parse_role_groups

        parse_role_groups(self.AUTH_ROLE_GROUPS)
        self.AUTH_DEFAULT_ROLE = normalize_default_role(self.AUTH_DEFAULT_ROLE)
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

        # Change measurement windows (C3, MEA-10): the 7-day baseline and the 5-day post-window are the
        # minimums a verdict needs; only an explicit accelerated (demo/quickstart) setting may shorten them
        # (1 hour, 30 minutes). A shorter value is raised to the minimum with a warning, never a startup
        # failure: a setting that was valid before must not stop GreenKube from starting.
        accelerated = self.CHANGE_MEASUREMENT_ACCELERATED
        self.CHANGE_MEASUREMENT_PRE_WINDOW_DAYS = _clamp_window(
            "CHANGE_MEASUREMENT_PRE_WINDOW_DAYS",
            self.CHANGE_MEASUREMENT_PRE_WINDOW_DAYS,
            ACCELERATED_MIN_PRE_WINDOW_DAYS if accelerated else 7.0,
            "1 hour with CHANGE_MEASUREMENT_ACCELERATED"
            if accelerated
            else "7 days so every hour of the week is matched",
        )
        self.CHANGE_MEASUREMENT_POST_WINDOW_DAYS = _clamp_window(
            "CHANGE_MEASUREMENT_POST_WINDOW_DAYS",
            self.CHANGE_MEASUREMENT_POST_WINDOW_DAYS,
            ACCELERATED_MIN_POST_WINDOW_DAYS if accelerated else MIN_POST_WINDOW_DAYS,
            "30 minutes with CHANGE_MEASUREMENT_ACCELERATED"
            if accelerated
            else "a verdict needs 5 complete days of aligned post-change hours",
        )
        if self.CHANGE_MEASUREMENT_ACCELERATED:
            logging.getLogger(__name__).warning(
                "CHANGE_MEASUREMENT_ACCELERATED is on: change measurements use demo windows, 5-minute slots, "
                "a flat baseline-mean counterfactual and demo thresholds; their verdicts are labelled "
                "representative=false. Do not use it in production."
            )

        # Cross-field validation
        if self.DB_TYPE == "postgres" and not self.DB_CONNECTION_STRING:
            raise ValueError("DB_CONNECTION_STRING must be set for postgres database")

        if self.ELECTRICITY_PROVIDER == "electricity_maps" and not self.ELECTRICITY_MAPS_TOKEN:
            logging.warning(
                "⚠️  ELECTRICITY_MAPS_TOKEN is not set. CO2 figures will use static fallback data "
                "which may be inaccurate. Get a free token at https://www.electricitymaps.com/"
            )
        elif self.ELECTRICITY_PROVIDER == "wattnet" and (not self.WATTNET_EMAIL or not self.WATTNET_PASSWORD):
            logging.warning(
                "⚠️  ELECTRICITY_PROVIDER is set to 'wattnet' but WATTNET_EMAIL/WATTNET_PASSWORD are not set. "
                "CO2 figures will use static fallback data. Register at https://wattnet.eu to get credentials."
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

    _SERVICE_ACCOUNT_DIR: ClassVar[str] = "/var/run/secrets/kubernetes.io/serviceaccount"
    _CLUSTER_NAME_LABELS: ClassVar[tuple[str, ...]] = (
        "alpha.eksctl.io/cluster-name",
        "eks.amazonaws.com/cluster-name",
        "cluster.x-k8s.io/cluster-name",
        "cloud.google.com/gke-cluster-name",
        "aks.azure.com/cluster-name",
        "minikube.k8s.io/name",
    )

    @classmethod
    def _list_nodes_in_cluster(cls) -> Optional[tuple[list[dict], bool]]:
        """Up to two nodes and whether more exist, read with the pod's service account.

        Synchronous (configuration is loaded outside the event loop) and bounded
        to 5 seconds. None out of cluster or on any error.
        """
        host = os.getenv("KUBERNETES_SERVICE_HOST", "")
        token_path = os.path.join(cls._SERVICE_ACCOUNT_DIR, "token")
        if not host or not os.path.isfile(token_path):
            return None
        try:
            import httpx

            with open(token_path, encoding="utf-8") as handle:
                token = handle.read().strip()
            ca_path = os.path.join(cls._SERVICE_ACCOUNT_DIR, "ca.crt")
            if ":" in host:
                host = f"[{host}]"
            port = os.getenv("KUBERNETES_SERVICE_PORT", "443")
            response = httpx.get(
                f"https://{host}:{port}/api/v1/nodes",
                params={"limit": "2"},
                headers={"Authorization": f"Bearer {token}"},
                verify=ca_path if os.path.isfile(ca_path) else True,
                timeout=5.0,
            )
            response.raise_for_status()
            body = response.json()
            items = [item for item in body.get("items") or [] if isinstance(item, dict)]
            more = bool((body.get("metadata") or {}).get("continue")) or len(items) > 1
            return items, more
        except Exception as exc:
            logging.getLogger(__name__).debug("Could not list nodes to detect the cluster name: %s", exc)
            return None

    @classmethod
    def _auto_detect_cluster_name(cls) -> str:
        """Auto-detect a cluster name that every GreenKube pod agrees on.

        The name keys collection watermarks, GitOps bindings, savings and the
        ledger identity, so it must not depend on the node a pod runs on.
        Detection order (the nodes are read with the pod's service account):
        1. A single-node cluster (minikube, kind, k3s): its node name.
        2. A well-known cluster-name label on a node (EKS, GKE, AKS, Cluster API).
        3. Any other multi-node cluster: ``"default"``; set config.clusterName
           for a readable name.
        4. Nodes unreadable (no RBAC, out of cluster): ``K8S_NODE_NAME``
           (Downward API), else ``"default"``.

        Never raises — detection failures are logged at DEBUG level.
        """
        logger = logging.getLogger(__name__)

        listing = cls._list_nodes_in_cluster()
        if listing is not None:
            nodes, more = listing
            if len(nodes) == 1 and not more:
                name = (nodes[0].get("metadata") or {}).get("name")
                if name:
                    logger.debug("Auto-detected cluster name from the only node: %s", name)
                    return str(name)
            for node in nodes:
                labels = (node.get("metadata") or {}).get("labels") or {}
                for label in cls._CLUSTER_NAME_LABELS:
                    if labels.get(label):
                        logger.debug("Auto-detected cluster name from label %s: %s", label, labels[label])
                        return str(labels[label])
            if nodes:
                logger.info(
                    "Multi-node cluster without a cluster-name node label: using cluster name 'default' "
                    "(set config.clusterName / CLUSTER_NAME for a readable name)."
                )
                return "default"

        node_name = os.getenv("K8S_NODE_NAME", "")
        if node_name:
            logger.debug("Auto-detected cluster name from K8S_NODE_NAME: %s", node_name)
            return node_name

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

    @property
    def automation_delivery_paused(self) -> bool:
        """True when no pull request may be opened (``pauseDelivery`` or a 0.3.x off switch)."""
        return (
            bool(self.AUTOMATION_PAUSE_DELIVERY)
            or self.AUTOMATION_ENABLED is False
            or self.AUTOMATION_MODE == "disabled"
        )

    @property
    def automation_repository_allowlist(self) -> tuple[str, ...]:
        """Configured repository allowlist patterns; ``@discovered`` is expanded at run time."""
        return parse_repository_allowlist(self.AUTOMATION_REPOSITORY_ALLOWLIST)

    @property
    def helm_chart_hosts(self) -> tuple[str, ...]:
        """Hosts the controller may fetch remote Helm charts from; empty means discovered hosts."""
        return parse_helm_chart_hosts(self.GITOPS_HELM_CHART_HOSTS)

    @property
    def gitops_discovery_interval_seconds(self) -> float:
        return parse_gitops_duration_seconds(self.GITOPS_DISCOVERY_INTERVAL)

    @property
    def gitops_binding_ttl(self) -> timedelta:
        return timedelta(seconds=parse_gitops_duration_seconds(self.GITOPS_BINDING_TTL))

    @property
    def gitops_local_cluster_aliases(self) -> tuple[str, ...]:
        """Extra Argo CD server URLs or cluster names of the local cluster (delivery safety)."""
        return parse_local_cluster_aliases(self.GITOPS_LOCAL_CLUSTER_ALIASES)

    @property
    def intensity_max_age(self) -> timedelta:
        """Maximum age of a stored carbon intensity before the zone default is used."""
        return timedelta(seconds=parse_gitops_duration_seconds(self.INTENSITY_MAX_AGE))

    @property
    def recommendation_scan_interval(self) -> timedelta:
        """Interval between scheduled, leader-gated recommendation scans."""
        return timedelta(seconds=_scan_interval_seconds(self.RECOMMENDATION_SCAN_INTERVAL))

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
