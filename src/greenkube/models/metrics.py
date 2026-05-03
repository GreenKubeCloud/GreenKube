# src/greenkube/models/metrics.py
"""
This module defines the Pydantic data models for all metrics collected and
calculated within the GreenKube application. These models serve as the
single source of truth for our data structures, ensuring type safety and
consistency across all modules (collectors, calculators, reporters).
"""

from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field


class EnergyMetric(BaseModel):
    """
    Represents a single energy consumption data point, typically collected
    via Prometheus and estimated by the in-repo estimator. Now includes metadata for granular calculations.
    """

    pod_name: str = Field(..., description="The name of the Kubernetes pod.")
    namespace: str = Field(..., description="The namespace the pod belongs to.")
    joules: float = Field(..., description="The energy consumed by the pod in Joules over a period.")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="The timestamp of the measurement.",
    )
    node: Optional[str] = Field(None, description="The node where the pod is running.")
    region: Optional[str] = Field(None, description="The cloud region of the node.")
    is_estimated: bool = Field(False, description="Whether the metric relies on estimated values.")
    estimation_reasons: List[str] = Field(default_factory=list, description="Reasons for estimation.")


class CostMetric(BaseModel):
    """
    Represents a single cost data point, typically collected from a source
    like OpenCost.
    """

    pod_name: str = Field(..., description="The name of the Kubernetes pod.")
    namespace: str = Field(..., description="The namespace the pod belongs to.")
    cpu_cost: float = Field(..., description="The calculated cost of CPU usage for the pod.")
    ram_cost: float = Field(..., description="The calculated cost of RAM usage for the pod.")
    total_cost: float = Field(..., description="The total cost for the pod (CPU + RAM + other costs).")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="The timestamp of the cost calculation.",
    )


class CarbonEmissionMetric(BaseModel):
    """
    Represents a calculated carbon emission data point. This is not directly
    collected but is derived from EnergyMetrics.
    """

    pod_name: str = Field(..., description="The name of the Kubernetes pod.")
    namespace: str = Field(..., description="The namespace the pod belongs to.")
    co2e_grams: float = Field(..., description="The calculated carbon emissions in grams of CO2 equivalent.")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="The timestamp of the calculation.",
    )


class PodMetric(BaseModel):
    """
    Resource requests for a specific container, collected from K8s API.
    """

    pod_name: str = Field(..., description="The name of the Kubernetes pod.")
    namespace: str = Field(..., description="The namespace the pod belongs to.")
    container_name: str = Field(..., description="The name of the container within the pod.")
    cpu_request: int = Field(..., description="CPU request in millicores.")
    memory_request: int = Field(..., description="Memory request in bytes.")
    ephemeral_storage_request: int = Field(0, description="Ephemeral storage request in bytes.")
    owner_kind: Optional[str] = Field(None, description="Owner resource kind (Deployment, StatefulSet, etc.).")
    owner_name: Optional[str] = Field(None, description="Owner resource name.")


class RecommendationType(str, Enum):
    """Enumeration of possible recommendation types."""

    ZOMBIE_POD = "ZOMBIE_POD"
    RIGHTSIZING_CPU = "RIGHTSIZING_CPU"
    RIGHTSIZING_MEMORY = "RIGHTSIZING_MEMORY"
    AUTOSCALING_CANDIDATE = "AUTOSCALING_CANDIDATE"
    OFF_PEAK_SCALING = "OFF_PEAK_SCALING"
    IDLE_NAMESPACE = "IDLE_NAMESPACE"
    CARBON_AWARE_SCHEDULING = "CARBON_AWARE_SCHEDULING"
    OVERPROVISIONED_NODE = "OVERPROVISIONED_NODE"
    UNDERUTILIZED_NODE = "UNDERUTILIZED_NODE"


class RecommendationStatus(str, Enum):
    """Lifecycle status of a persisted recommendation."""

    ACTIVE = "active"
    APPLIED = "applied"
    IGNORED = "ignored"
    STALE = "stale"


class Recommendation(BaseModel):
    """Represents a single actionable optimization recommendation."""

    pod_name: Optional[str] = Field(
        None, description="The name of the target pod (None for namespace/node-level recs)."
    )
    namespace: Optional[str] = Field(None, description="The namespace of the target (None for node-level recs).")
    type: RecommendationType = Field(..., description="The category of the recommendation.")
    description: str = Field(..., description="A human-readable description of the recommendation.")
    reason: str = Field("", description="Human-readable explanation of why the recommendation was made.")
    priority: str = Field("medium", description="Priority level: high, medium, or low.")
    scope: str = Field(
        "pod",
        description="Recommendation scope: 'pod', 'workload', 'namespace', or 'node'.",
    )
    potential_savings_co2e_grams: Optional[float] = Field(
        None, description="Estimated CO2e savings in grams if implemented."
    )
    potential_savings_cost: Optional[float] = Field(None, description="Estimated cost savings if implemented.")
    current_cpu_request_millicores: Optional[int] = Field(None, description="Current CPU request in millicores.")
    recommended_cpu_request_millicores: Optional[int] = Field(
        None, description="Recommended CPU request in millicores (floored to the configured minimum)."
    )
    current_memory_request_bytes: Optional[int] = Field(None, description="Current memory request in bytes.")
    recommended_memory_request_bytes: Optional[int] = Field(
        None, description="Recommended memory request in bytes (floored to the configured minimum)."
    )
    cron_schedule: Optional[str] = Field(None, description="Suggested cron schedule for off-peak scaling.")
    target_node: Optional[str] = Field(None, description="Target node for node-level recommendations.")


class RecommendationRecord(BaseModel):
    """A persisted recommendation snapshot for historical tracking."""

    id: Optional[int] = Field(None, description="Auto-generated database ID.")
    pod_name: Optional[str] = Field(None, description="The name of the target pod.")
    namespace: Optional[str] = Field(None, description="The namespace of the target.")
    type: RecommendationType = Field(..., description="The category of the recommendation.")
    description: str = Field(..., description="A human-readable description of the recommendation.")
    reason: str = Field("", description="Human-readable explanation of why the recommendation was made.")
    priority: str = Field("medium", description="Priority level: high, medium, or low.")
    scope: str = Field("pod", description="Recommendation scope: 'pod', 'workload', 'namespace', or 'node'.")
    status: RecommendationStatus = Field(RecommendationStatus.ACTIVE, description="Lifecycle status.")
    potential_savings_cost: Optional[float] = Field(None, description="Estimated cost savings if implemented.")
    potential_savings_co2e_grams: Optional[float] = Field(
        None, description="Estimated CO2e savings in grams if implemented."
    )
    current_cpu_request_millicores: Optional[int] = Field(None, description="Current CPU request in millicores.")
    recommended_cpu_request_millicores: Optional[int] = Field(
        None, description="Recommended CPU request in millicores."
    )
    current_memory_request_bytes: Optional[int] = Field(None, description="Current memory request in bytes.")
    recommended_memory_request_bytes: Optional[int] = Field(None, description="Recommended memory request in bytes.")
    cron_schedule: Optional[str] = Field(None, description="Suggested cron schedule for off-peak scaling.")
    target_node: Optional[str] = Field(None, description="Target node for node-level recommendations.")
    # Applied lifecycle fields
    applied_at: Optional[datetime] = Field(None, description="When the recommendation was applied.")
    actual_cpu_request_millicores: Optional[int] = Field(
        None, description="Actual CPU value applied (may differ from recommended)."
    )
    actual_memory_request_bytes: Optional[int] = Field(
        None, description="Actual memory value applied (may differ from recommended)."
    )
    carbon_saved_co2e_grams: Optional[float] = Field(
        None, description="Actual CO2e savings realised after applying the recommendation."
    )
    cost_saved: Optional[float] = Field(None, description="Actual cost savings realised after applying.")
    # Ignore lifecycle fields
    ignored_at: Optional[datetime] = Field(None, description="When the recommendation was ignored.")
    ignored_reason: Optional[str] = Field(None, description="Reason for ignoring the recommendation.")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the recommendation was generated.",
    )
    updated_at: Optional[datetime] = Field(None, description="When the recommendation was last updated.")

    @classmethod
    def from_recommendation(
        cls, rec: "Recommendation", created_at: Optional[datetime] = None
    ) -> "RecommendationRecord":
        """Creates a RecommendationRecord from a Recommendation.

        Args:
            rec: The source Recommendation object.
            created_at: Optional timestamp override; defaults to now (UTC).

        Returns:
            A new RecommendationRecord instance.
        """
        return cls(
            pod_name=rec.pod_name,
            namespace=rec.namespace,
            type=rec.type,
            description=rec.description,
            reason=rec.reason,
            priority=rec.priority,
            scope=rec.scope,
            status=RecommendationStatus.ACTIVE,
            potential_savings_cost=rec.potential_savings_cost,
            potential_savings_co2e_grams=rec.potential_savings_co2e_grams,
            current_cpu_request_millicores=rec.current_cpu_request_millicores,
            recommended_cpu_request_millicores=rec.recommended_cpu_request_millicores,
            current_memory_request_bytes=rec.current_memory_request_bytes,
            recommended_memory_request_bytes=rec.recommended_memory_request_bytes,
            cron_schedule=rec.cron_schedule,
            target_node=rec.target_node,
            created_at=created_at or datetime.now(timezone.utc),
        )


class ApplyRecommendationRequest(BaseModel):
    """Request body for marking a recommendation as applied."""

    actual_cpu_request_millicores: Optional[int] = Field(
        None, description="Actual CPU value applied (may differ from recommended)."
    )
    actual_memory_request_bytes: Optional[int] = Field(
        None, description="Actual memory value applied (may differ from recommended)."
    )
    carbon_saved_co2e_grams: Optional[float] = Field(
        None, description="Actual CO2e savings realised (computed server-side if omitted)."
    )
    cost_saved: Optional[float] = Field(
        None, description="Actual cost savings realised (computed server-side if omitted)."
    )


class IgnoreRecommendationRequest(BaseModel):
    """Request body for permanently ignoring a recommendation."""

    reason: Optional[str] = Field(None, description="Reason for ignoring the recommendation.")


class RecommendationSavingsSummary(BaseModel):
    """Aggregate savings from all applied recommendations."""

    total_carbon_saved_co2e_grams: float = Field(0.0, description="Total CO2e saved in grams.")
    total_cost_saved: float = Field(0.0, description="Total cost saved.")
    applied_count: int = Field(0, description="Number of recommendations marked as applied.")
    namespace_breakdown: List[dict] = Field(default_factory=list, description="Savings breakdown per namespace.")


class MetricsSummaryRow(BaseModel):
    """
    A single pre-computed summary row for a specific time window.

    These rows are maintained in the ``metrics_summary`` table and updated
    hourly by the :class:`~greenkube.core.summary_refresher.SummaryRefresher`
    so that the frontend can load KPI data instantly without scanning
    millions of raw metric rows.
    """

    window_slug: str = Field(
        ...,
        description=(
            "Identifier for the time window. "
            "Built-in slugs: '1h', '6h', '24h', '7d', '30d', '1y', 'ytd'. "
            "Prefixed with '<namespace>/' when scoped to a namespace."
        ),
    )
    namespace: Optional[str] = Field(
        None,
        description="Kubernetes namespace, or None for cluster-wide aggregation.",
    )
    total_co2e_grams: float = Field(
        0.0,
        description="GHG Scope 2 — operational electricity emissions in grams CO₂e.",
    )
    total_embodied_co2e_grams: float = Field(
        0.0,
        description="GHG Scope 3 (Cat. 1) — hardware manufacturing emissions in grams CO₂e.",
    )
    total_co2e_all_scopes: float = Field(
        0.0,
        description="GHG Scope 2 + Scope 3 — total carbon footprint in grams CO₂e.",
    )
    total_cost: float = Field(0.0, description="Total cost in dollars.")
    total_energy_joules: float = Field(0.0, description="Total energy in Joules.")
    pod_count: int = Field(0, description="Number of unique pods.")
    namespace_count: int = Field(0, description="Number of unique namespaces.")
    updated_at: Optional[datetime] = Field(
        None,
        description="Timestamp of the last refresh.",
    )


class TimeseriesCachePoint(BaseModel):
    """
    A single pre-computed time-series bucket stored in ``metrics_timeseries_cache``.

    The table holds one row per (window_slug, namespace, bucket_ts) so the
    frontend can retrieve chart data with a single lightweight indexed query.
    Buckets are hourly for ``24h`` and daily for all longer windows.
    """

    window_slug: str = Field(..., description="The parent time window slug (e.g. '7d', 'ytd').")
    namespace: Optional[str] = Field(None, description="Namespace, or None for cluster-wide.")
    bucket_ts: str = Field(..., description="ISO-8601 bucket timestamp (UTC).")
    co2e_grams: float = Field(0.0, description="GHG Scope 2 — electricity emissions in grams CO₂e for this bucket.")
    embodied_co2e_grams: float = Field(
        0.0,
        description="GHG Scope 3 (Cat. 1) — hardware manufacturing emissions in grams CO₂e for this bucket.",
    )
    total_co2e_all_scopes: float = Field(
        0.0,
        description="GHG Scope 2 + Scope 3 — total carbon footprint in grams CO₂e for this bucket.",
    )
    total_cost: float = Field(0.0, description="Total cost for this bucket.")
    joules: float = Field(0.0, description="Total energy in Joules for this bucket.")


class EnvironmentalMetric(BaseModel):
    """
    Holds environmental factors for a specific location (e.g., a cloud region).
    """

    pue: float = Field(..., description="Power Usage Effectiveness of the data center.")
    grid_intensity: float = Field(..., description="Carbon intensity of the grid in gCO2e/kWh.")


class CombinedMetric(BaseModel):
    """
    A combined data model for reporting AND analysis.
    This links all collected and calculated data for a single pod.
    """

    pod_name: str
    namespace: str

    # From CostMetric
    total_cost: float = 0.0

    # From CarbonEmissionMetric
    co2e_grams: float = Field(
        0.0,
        description=(
            "GHG Scope 2 — indirect emissions from purchased electricity "
            "(grid carbon intensity × energy consumed × PUE), in grams CO₂e."
        ),
    )

    # From EnvironmentalMetric
    pue: float = 1.0
    grid_intensity: float = 0.0

    # From EnergyMetric
    joules: float = 0.0

    # From PodMetric (Note: This may be an aggregation of containers)
    cpu_request: int = 0  # in millicores
    memory_request: int = 0  # in bytes
    # Actual usage metrics (from Prometheus)
    sample_count: int = Field(1, description="Number of raw samples represented by this metric point.")
    cpu_usage_millicores: Optional[int] = Field(None, description="Actual CPU usage in millicores.")
    cpu_usage_max_millicores: Optional[int] = Field(
        None,
        description="Maximum CPU usage in millicores represented by this metric point.",
    )
    memory_usage_bytes: Optional[int] = Field(None, description="Actual memory working set in bytes.")
    memory_usage_max_bytes: Optional[int] = Field(
        None,
        description="Maximum memory working set in bytes represented by this metric point.",
    )
    # Network I/O metrics (from Prometheus)
    network_receive_bytes: Optional[float] = Field(None, description="Network bytes received per second (rate).")
    network_transmit_bytes: Optional[float] = Field(None, description="Network bytes transmitted per second (rate).")
    # Disk I/O metrics (from Prometheus)
    disk_read_bytes: Optional[float] = Field(None, description="Disk bytes read per second (rate).")
    disk_write_bytes: Optional[float] = Field(None, description="Disk bytes written per second (rate).")
    # Storage metrics
    storage_request_bytes: Optional[int] = Field(None, description="PVC storage requested in bytes.")
    storage_usage_bytes: Optional[int] = Field(None, description="Actual PVC storage usage in bytes.")
    ephemeral_storage_request_bytes: Optional[int] = Field(None, description="Ephemeral storage requested in bytes.")
    ephemeral_storage_usage_bytes: Optional[int] = Field(None, description="Actual ephemeral storage usage in bytes.")
    # GPU metrics
    gpu_usage_millicores: Optional[int] = Field(None, description="GPU usage in millicores (from DCGM/nvidia-smi).")
    # Stability metrics
    restart_count: Optional[int] = Field(None, description="Total container restart count for the pod.")
    # Ownership metadata
    owner_kind: Optional[str] = Field(None, description="Owner resource kind (Deployment, StatefulSet, etc.).")
    owner_name: Optional[str] = Field(None, description="Owner resource name.")
    # Optional aggregation period (e.g., '2025-11' or '2025')
    period: Optional[str] = None
    # Timestamp for the metric window start (e.g., from Prometheus)
    timestamp: Optional[datetime] = None
    # Duration of the metric window in seconds
    duration_seconds: Optional[int] = None
    # Timestamp of the grid intensity data used for calculation
    grid_intensity_timestamp: Optional[datetime] = None
    # Node where the pod is running
    node: Optional[str] = None
    # Metadata for historical accuracy
    node_instance_type: Optional[str] = None
    node_zone: Optional[str] = Field(None, description="Cloud provider zone")
    emaps_zone: Optional[str] = Field(None, description="Electricity Maps zone")
    is_estimated: Optional[bool] = Field(False, description="Whether the metric relies on estimated values.")
    estimation_reasons: List[str] = Field(default_factory=list, description="Reasons for estimation.")
    embodied_co2e_grams: Optional[float] = Field(
        0.0,
        description=(
            "GHG Scope 3 (Category 1: Purchased Goods & Services) — upstream hardware "
            "manufacturing emissions allocated to this pod by CPU share, in grams CO₂e."
        ),
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_co2e_grams(self) -> float:
        """GHG Scope 2 + Scope 3 — total carbon footprint for this pod in grams CO₂e."""
        return (self.co2e_grams or 0.0) + (self.embodied_co2e_grams or 0.0)

    # Calculation methodology version — allows detecting stale data after algorithm changes
    calculation_version: Optional[str] = Field(
        None, description="Version of the calculation algorithm that produced this metric."
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)
