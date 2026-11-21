# src/greenkube/api/schemas.py
"""
Pydantic response schemas for the API.
Keeps API-specific response shapes separate from internal domain models.
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from greenkube.gitops.contracts import (
    BindingStatus,
    ControllerKind,
    OperationStatus,
    PlanStatus,
    RendererKind,
)
from greenkube.gitops.sha import GIT_OBJECT_SHA, SHA256_DIGEST
from greenkube.models.metrics import CombinedMetric, MetricsSummaryRow, TimeseriesCachePoint
from greenkube.models.node import NodeInfo


class GitOpsResponseModel(BaseModel):
    """Base model for explicit, secret-safe GitOps API responses."""

    model_config = ConfigDict(extra="forbid")


class GitOpsCapabilityResponse(GitOpsResponseModel):
    """Availability of one Kubernetes controller or source CRD."""

    available: bool
    reason: Optional[str] = None


class GitOpsDiscoveryCapabilitiesResponse(GitOpsResponseModel):
    """Capabilities reported by the Kubernetes discovery source."""

    argocd: GitOpsCapabilityResponse
    flux_kustomization: GitOpsCapabilityResponse
    flux_helm_release: GitOpsCapabilityResponse
    flux_source_git_repository: GitOpsCapabilityResponse
    flux_source_oci_repository: GitOpsCapabilityResponse
    flux_source_helm_repository: GitOpsCapabilityResponse
    flux_source_bucket: GitOpsCapabilityResponse


class GitOpsCoverageResponse(GitOpsResponseModel):
    """Freshness-aware workload binding counts by resolution status."""

    total: int = Field(ge=0)
    resolved: int = Field(ge=0)
    ambiguous: int = Field(ge=0)
    unresolved: int = Field(ge=0)
    blocked: int = Field(ge=0)
    stale: int = Field(ge=0)
    ratio: float = Field(ge=0, le=1)


class GitOpsDiscoveryStatusResponse(GitOpsResponseModel):
    """Read-only controller capability status and most recent binding coverage."""

    cluster: str
    discovery: GitOpsDiscoveryCapabilitiesResponse
    last_refresh: Optional[datetime] = None
    coverage: GitOpsCoverageResponse


class GitOpsDiscoveryRefreshResponse(GitOpsResponseModel):
    """Outcome of one explicit discovery refresh."""

    cluster: str
    refreshed_at: datetime
    coverage: GitOpsCoverageResponse


class GitOpsWorkloadSourceResponse(GitOpsResponseModel):
    """Safe API projection of a discovered source candidate."""

    id: str
    controller: str
    controller_kind: ControllerKind
    controller_name: str
    controller_namespace: str
    provider_id: str
    repo_url: str
    path: str
    renderer: RendererKind
    confidence: float = Field(ge=0, le=1)
    status: BindingStatus
    reason: Optional[str] = None
    revision: Optional[str] = None
    source_revision: Optional[str] = None
    observed_at: datetime
    expires_at: Optional[datetime] = None
    generation_token: StrictStr = Field(pattern=rf"^{SHA256_DIGEST.pattern}$")


class GitOpsBindingProvenanceResponse(GitOpsResponseModel):
    """Safe source/controller provenance for one binding candidate."""

    source_id: str
    controller: str
    controller_kind: ControllerKind
    controller_name: str
    controller_namespace: str
    provider_id: str
    repo_url: str
    path: str
    renderer: RendererKind
    revision: Optional[str] = None
    source_revision: Optional[str] = None
    observed_at: datetime


class GitOpsWorkloadBindingResponse(GitOpsResponseModel):
    """One workload binding, its candidates, resolver reason and provenance."""

    cluster: str
    namespace: str
    workload_kind: str
    workload_name: str
    status: BindingStatus
    confidence: float = Field(ge=0, le=1)
    binding: Optional[GitOpsWorkloadSourceResponse] = None
    candidates: List[GitOpsWorkloadSourceResponse] = Field(default_factory=list)
    candidates_truncated: bool = False
    reason: Optional[str] = None
    provenance: List[GitOpsBindingProvenanceResponse] = Field(default_factory=list)
    observed_at: datetime
    expires_at: Optional[datetime] = None


class GitOpsWorkloadBindingListResponse(GitOpsResponseModel):
    """Bounded page of safe workload binding projections."""

    items: List[GitOpsWorkloadBindingResponse] = Field(max_length=100)
    limit: int = Field(ge=1, le=100)
    has_more: bool
    next_cursor: Optional[StrictStr] = None


class GitOpsManualBindingResolutionRequest(GitOpsResponseModel):
    """Explicit confirmation of one current server-returned candidate."""

    candidate_id: StrictStr = Field(min_length=1, max_length=256)
    generation_token: StrictStr = Field(pattern=rf"^{SHA256_DIGEST.pattern}$")
    confirmed: Literal[True]


class GitOpsErrorResponse(GitOpsResponseModel):
    """Stable error body for documented GitOps API errors."""

    detail: str


class AutomationPlanValuesMappingRequest(GitOpsResponseModel):
    """One-time Argo Helm values mapping; repository identity remains server-resolved."""

    file_path: StrictStr = Field(min_length=1, max_length=1024)
    container: StrictStr = Field(min_length=1, max_length=253)
    path: List[StrictStr] = Field(min_length=1, max_length=32)

    @field_validator("file_path", "container")
    @classmethod
    def validate_canonical_value(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("mapping file_path and container must be non-blank and canonical")
        return value

    @field_validator("path")
    @classmethod
    def validate_resources_path(cls, value: List[str]) -> List[str]:
        if any(not segment.strip() or segment != segment.strip() for segment in value) or value[-1] != "resources":
            raise ValueError("mapping path must contain canonical keys and end in resources")
        return value


class AutomationPlanMappingOptionsResponse(GitOpsResponseModel):
    """Server-resolved choices for a one-time ambiguous Helm mapping."""

    files: List[StrictStr] = Field(min_length=1, max_length=100)
    containers: List[StrictStr] = Field(min_length=1, max_length=32)


class AutomationPlanCreateRequest(GitOpsResponseModel):
    """Recommendation IDs and an optional mapping; action/source inputs stay server-side."""

    recommendation_ids: List[StrictInt] = Field(min_length=1, max_length=100)
    values_mapping: Optional[AutomationPlanValuesMappingRequest] = None

    @field_validator("recommendation_ids")
    @classmethod
    def validate_recommendation_ids(cls, value: List[int]) -> List[int]:
        if any(recommendation_id <= 0 for recommendation_id in value):
            raise ValueError("recommendation_ids must contain positive integers")
        if len(value) != len(set(value)):
            raise ValueError("recommendation_ids must be unique")
        return value


class AutomationPlanApproveRequest(GitOpsResponseModel):
    """Approval for the exact digest returned by plan creation or read."""

    digest: StrictStr = Field(pattern=rf"^{SHA256_DIGEST.pattern}$")


class AutomationPlanSourceResponse(GitOpsResponseModel):
    """Safe source summary that omits provider configuration and resolver evidence."""

    controller: str
    repo: str
    base_branch: str
    base_revision: Optional[str] = None
    path: str
    renderer: RendererKind


class AutomationPlanFileResponse(GitOpsResponseModel):
    """Public file-edit metadata without repository file contents."""

    path: str
    operation: Literal["create", "update", "delete"]
    original_sha: Optional[str] = None


class AutomationPlanValidationResponse(GitOpsResponseModel):
    """Safe render-validation summary without internal warnings or evidence."""

    passed: bool
    target_changed: bool
    unrelated_changes: bool
    requests_within_limits: bool
    renderer: RendererKind
    output_digest: str


class AutomationPlanResponse(GitOpsResponseModel):
    """Explicit plan projection; never serializes the internal action/source models."""

    plan_id: str
    recommendation_ids: List[int]
    status: PlanStatus
    #: Reason code of the last status change (``cancelled_by_user``, ``plan_expired``...), F-10.
    status_reason: Optional[str] = None
    digest: Optional[str] = None
    policy_result: Dict[str, Any] = Field(default_factory=dict)
    operation_id: Optional[str] = None
    operation_status: Optional[OperationStatus] = None
    rollback_of_plan_id: Optional[str] = None
    source: AutomationPlanSourceResponse
    files: List[AutomationPlanFileResponse] = Field(default_factory=list)
    diff: Optional[str] = None
    rendered_diff: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)
    warnings_truncated: bool = False
    validation: Optional[AutomationPlanValidationResponse] = None
    created_at: datetime
    expires_at: Optional[datetime] = None
    #: How the approved plan is delivered (F4): ``direct``, ``patch`` or ``ci``; None before approval.
    delivery_mode: Optional[Literal["direct", "patch", "ci"]] = None
    #: True when ``automation.autoApprove`` approved the plan with its preview (F5).
    auto_approved: bool = False
    #: Why ``automation.autoApprove`` did not approve the preview (empty when it is off or approved).
    auto_approve_blockers: List[str] = Field(default_factory=list)


class AutomationPlanDeliveryRequest(GitOpsResponseModel):
    """A pull request opened outside GreenKube for an approved plan (``patch`` and ``ci`` modes, F4)."""

    pr_url: StrictStr = Field(min_length=8, max_length=2048)
    #: Head commit of the pull request; required in ``ci`` mode, optional for a ``patch`` link-back.
    head_sha: Optional[StrictStr] = Field(default=None, pattern=rf"^(?:{GIT_OBJECT_SHA.pattern})$")


class AutomationPlanDeliveryPullRequest(GitOpsResponseModel):
    number: int
    url: Optional[str] = None
    head_sha: Optional[str] = None
    #: ``branch`` (deterministic plan branch), ``trailer`` (GreenKube-Plan trailer) or ``recorded``.
    linked_by: str


class AutomationPlanDeliveryResponse(GitOpsResponseModel):
    plan: AutomationPlanResponse
    pull_request: AutomationPlanDeliveryPullRequest
    #: False when the same pull request had already been recorded (idempotent report).
    created: bool


class AutomationPlanDeliveryItem(GitOpsResponseModel):
    """One approved plan awaiting external delivery, with what a CI job needs to deliver it."""

    plan_id: str
    status: PlanStatus
    delivery_mode: Optional[Literal["direct", "patch", "ci"]] = None
    digest: Optional[str] = None
    recommendation_ids: List[int]
    repository: str
    base_branch: str
    base_revision: Optional[str] = None
    #: Deterministic branch the pull request must come from.
    branch: Optional[str] = None
    #: ``GET`` path of the ``git format-patch`` mailbox.
    patch_url: str
    #: ``POST`` path of the delivery report.
    delivery_url: str
    title: Optional[str] = None
    body: Optional[str] = None
    created_at: datetime
    expires_at: Optional[datetime] = None


class AutomationPlanErrorResponse(GitOpsResponseModel):
    """Stable, redacted error response for plan operations."""

    code: str
    detail: str
    #: The planner's own explanation (plan creation and rollback preview only), redacted and bounded.
    reason: Optional[str] = None
    policy_result: Optional[Dict[str, Any]] = None
    mapping_options: Optional[AutomationPlanMappingOptionsResponse] = None


class CollectionHoleResponse(BaseModel):
    """Consecutive uncovered steps of one status (COL-04)."""

    start: datetime
    end: datetime
    status: str = Field(..., description="gap, failed or unknown.")
    reason: Optional[str] = None


class CollectionCoverageResponse(BaseModel):
    """Coverage of a period by the metric collection (COL-04).

    ``coverage_ratio`` is the share of steps whose data is known: collected
    (rows stored) or empty (Prometheus scraped, no pod ran). Totals over a
    period with ``complete = false`` miss the uncovered steps.
    """

    cluster: str
    source: str
    start: datetime
    end: datetime
    step_seconds: int
    expected_steps: int
    collected_steps: int
    empty_steps: int
    failed_steps: int
    gap_steps: int
    unknown_steps: int
    coverage_ratio: float
    complete: bool
    holes: List[CollectionHoleResponse] = Field(default_factory=list)


class CollectionIdentityMismatchResponse(BaseModel):
    """A controller of another cluster refused this database: collection is stopped (COL-02 review)."""

    stored_uid: str = Field(..., description="kube-system UID this database belongs to.")
    live_uid: str = Field(..., description="kube-system UID of the controller that refused it.")
    seen_at: Optional[datetime] = None


class CollectionStatusResponse(BaseModel):
    """Watermark, lag and last hole of the metric collection (COL-04)."""

    watermark: Optional[datetime] = None
    lag_seconds: Optional[float] = None
    last_gap: Optional[CollectionHoleResponse] = None
    identity_mismatch: Optional[CollectionIdentityMismatchResponse] = Field(
        None,
        description="Set while collection refuses this database because it belongs to another cluster; "
        "set config.collection.allowClusterUidChange once to adopt it.",
    )


class CompressionStatusResponse(BaseModel):
    """Hourly compression watermark and lag (ROL-01)."""

    status: str = Field(
        ..., description="`ok`, or `degraded` when a compression watermark trails its reachable hour by over 2 h."
    )
    lag_seconds: Optional[float] = Field(
        None,
        description="Largest lag of the metric tables behind now - the effective compression age (the longer of "
        "METRICS_COMPRESSION_AGE_HOURS and COLLECTION_MAX_BACKFILL).",
    )
    watermarks: dict[str, Optional[datetime]] = Field(
        default_factory=dict, description="Compression watermark per raw metric table (exclusive end)."
    )


class HealthResponse(BaseModel):
    """Response schema for the health check endpoint."""

    status: str = Field(..., description="Health status of the API.")
    version: str = Field(..., description="Current application version.")
    collection: Optional[CollectionStatusResponse] = Field(
        None, description="Collection watermark, lag and last hole; absent when the database cannot be read."
    )
    demo: bool = Field(
        False, description="True when the API runs `greenkube demo` (synthetic data, pull requests disabled)."
    )
    compression: Optional[CompressionStatusResponse] = Field(
        None, description="Hourly compression watermark and lag; absent when the database cannot be read."
    )


class VersionResponse(BaseModel):
    """Response schema for the version endpoint."""

    version: str = Field(..., description="Current application version.")


class MetricsSummaryResponse(BaseModel):
    """Aggregated summary of metrics over a time range."""

    total_co2e_grams: float = Field(0.0, description="GHG Scope 2 — electricity emissions in grams CO₂e.")
    total_embodied_co2e_grams: float = Field(
        0.0, description="GHG Scope 3 (Cat. 1) — hardware manufacturing emissions in grams CO₂e."
    )
    total_co2e_all_scopes: float = Field(
        0.0, description="GHG Scope 2 + Scope 3 — total carbon footprint in grams CO₂e."
    )
    total_cost: float = Field(0.0, description="Total cost in dollars.")
    total_energy_joules: float = Field(0.0, description="Total energy in Joules.")
    pod_count: int = Field(0, description="Number of unique pods.")
    namespace_count: int = Field(0, description="Number of unique namespaces.")
    idle_co2e_grams: Optional[float] = Field(
        None,
        description="Scope 2 CO₂e of the node idle share (unreserved capacity, `__idle__` rows); null with a "
        "namespace filter or when the range holds no idle row.",
    )
    idle_embodied_co2e_grams: Optional[float] = Field(None, description="Scope 3 CO₂e of the node idle share.")
    idle_cost: Optional[float] = Field(None, description="Modeled cost of the node idle share.")
    idle_energy_joules: Optional[float] = Field(None, description="Energy of the node idle share in Joules.")
    cluster_total_co2e_grams: Optional[float] = Field(
        None, description="Scope 2 CO₂e of the cluster: allocated to workloads + idle share."
    )
    cluster_total_embodied_co2e_grams: Optional[float] = Field(
        None, description="Scope 3 CO₂e of the cluster: allocated + idle share."
    )
    cluster_total_cost: Optional[float] = Field(None, description="Cost of the cluster: allocated + idle share.")
    cluster_total_energy_joules: Optional[float] = Field(
        None, description="Energy of the cluster in Joules: allocated + idle share."
    )
    idle_data_since: Optional[datetime] = Field(
        None,
        description="First instant covered by stored idle rows. When the window holds pod rows before it, the "
        "idle and cluster-total fields are null (the idle history covers only part of the window).",
    )


class TimeseriesPoint(BaseModel):
    """A single data point in a time-series aggregation."""

    timestamp: str = Field(..., description="The bucket timestamp (ISO 8601 or week format).")
    co2e_grams: float = Field(0.0, description="GHG Scope 2 — electricity emissions in grams CO₂e for this bucket.")
    embodied_co2e_grams: float = Field(
        0.0, description="GHG Scope 3 (Cat. 1) — hardware manufacturing emissions in grams CO₂e for this bucket."
    )
    total_co2e_all_scopes: float = Field(
        0.0, description="GHG Scope 2 + Scope 3 — total carbon footprint in grams CO₂e for this bucket."
    )
    total_cost: float = Field(0.0, description="Total cost in dollars for this bucket.")
    joules: float = Field(0.0, description="Total energy in Joules for this bucket.")
    pod_count: int = Field(0, description="Number of unique pods in this bucket.")
    namespace_count: int = Field(0, description="Number of unique namespaces in this bucket.")
    idle_co2e_grams: Optional[float] = Field(
        None,
        description="Scope 2 CO₂e of the node idle share (unreserved capacity, `__idle__` rows); null with a "
        "namespace filter or when the range holds no idle row.",
    )
    idle_embodied_co2e_grams: Optional[float] = Field(None, description="Scope 3 CO₂e of the node idle share.")
    idle_cost: Optional[float] = Field(None, description="Modeled cost of the node idle share.")
    idle_energy_joules: Optional[float] = Field(None, description="Energy of the node idle share in Joules.")
    cluster_total_co2e_grams: Optional[float] = Field(
        None, description="Scope 2 CO₂e of the cluster: allocated to workloads + idle share."
    )
    cluster_total_embodied_co2e_grams: Optional[float] = Field(
        None, description="Scope 3 CO₂e of the cluster: allocated + idle share."
    )
    cluster_total_cost: Optional[float] = Field(None, description="Cost of the cluster: allocated + idle share.")
    cluster_total_energy_joules: Optional[float] = Field(
        None, description="Energy of the cluster in Joules: allocated + idle share."
    )


class ConfigResponse(BaseModel):
    """Non-sensitive configuration values."""

    db_type: str
    cloud_provider: str
    default_zone: str
    default_intensity: float
    default_pue: float
    log_level: str
    log_format: str
    normalization_granularity: str
    prometheus_query_range_step: str
    api_host: str
    api_port: int
    currency: str = Field(
        "USD", description="Pricing currency (ISO 4217) of every cost and savings amount (impact.pricing.currency)."
    )
    intensity_defaults_year: Optional[int] = Field(
        None,
        description=(
            "Calendar year of the bundled annual grid intensities used when no live value exists "
            "(intensity_source zone_default or country_default, NRG-04)."
        ),
    )


class PaginatedMetricsResponse(BaseModel):
    """Paginated list of combined metrics."""

    total: int = Field(..., description="Total number of matching records.")
    offset: int = Field(0, description="Number of records skipped.")
    limit: int = Field(1000, description="Maximum records in this page.")
    items: List[CombinedMetric] = Field(default_factory=list, description="The metrics in this page.")
    next_cursor: Optional[str] = Field(None, description="Opaque cursor for the next page.")


class NodeResponse(NodeInfo):
    """A node snapshot with where its GPU power came from in the latest collection (NRG-13)."""

    gpu_source: Optional[str] = Field(
        None,
        description=(
            "GPU power source of the node's latest idle row: dcgm, gpu_tdp_estimate or unmeasured. "
            "None when no GPU was detected or no recent row exists."
        ),
    )


class PaginatedNodesResponse(BaseModel):
    """Opt-in cursor-paginated node response."""

    items: List[NodeResponse] = Field(default_factory=list, description="Nodes in this page.")
    next_cursor: Optional[str] = Field(None, description="Opaque cursor for the next page.")


class NamespaceBreakdownItem(BaseModel):
    """Aggregated metrics for a single namespace."""

    namespace: str
    co2e_grams: float = 0.0
    embodied_co2e_grams: float = 0.0
    total_cost: float = 0.0
    energy_joules: float = 0.0


class TopPodItem(BaseModel):
    """Aggregated metrics for a single pod."""

    namespace: str
    pod_name: str
    co2e_grams: float = 0.0
    embodied_co2e_grams: float = 0.0
    total_cost: float = 0.0
    energy_joules: float = 0.0


class ReportSummaryResponse(BaseModel):
    """Preview summary for a report before export."""

    total_rows: int = Field(0, description="Total number of rows in the report.")
    total_co2e_grams: float = Field(0.0, description="GHG Scope 2 — electricity emissions in grams CO₂e.")
    total_embodied_co2e_grams: float = Field(
        0.0, description="GHG Scope 3 (Cat. 1) — hardware manufacturing emissions in grams CO₂e."
    )
    total_co2e_all_scopes: float = Field(
        0.0, description="GHG Scope 2 + Scope 3 — total carbon footprint in grams CO₂e."
    )
    total_cost: float = Field(0.0, description="Total cost in dollars.")
    total_energy_joules: float = Field(0.0, description="Total energy in Joules.")
    unique_pods: int = Field(0, description="Number of unique pods.")
    unique_namespaces: int = Field(0, description="Number of unique namespaces.")
    idle_co2e_grams: Optional[float] = Field(
        None,
        description="Scope 2 CO₂e of the node idle share (unreserved capacity, `__idle__` rows); null with a "
        "namespace filter or when the range holds no idle row.",
    )
    idle_embodied_co2e_grams: Optional[float] = Field(None, description="Scope 3 CO₂e of the node idle share.")
    idle_cost: Optional[float] = Field(None, description="Modeled cost of the node idle share.")
    idle_energy_joules: Optional[float] = Field(None, description="Energy of the node idle share in Joules.")
    cluster_total_co2e_grams: Optional[float] = Field(
        None, description="Scope 2 CO₂e of the cluster: allocated to workloads + idle share."
    )
    cluster_total_embodied_co2e_grams: Optional[float] = Field(
        None, description="Scope 3 CO₂e of the cluster: allocated + idle share."
    )
    cluster_total_cost: Optional[float] = Field(None, description="Cost of the cluster: allocated + idle share.")
    cluster_total_energy_joules: Optional[float] = Field(
        None, description="Energy of the cluster in Joules: allocated + idle share."
    )
    idle_data_since: Optional[datetime] = Field(
        None,
        description="First instant covered by stored idle rows. When the window holds pod rows before it, the "
        "idle and cluster-total fields are null (the idle history covers only part of the window).",
    )


class DashboardSummaryResponse(BaseModel):
    """Pre-computed KPI summaries for the frontend dashboard.

    Each key in *windows* is a window slug (e.g. ``'24h'``, ``'7d'``) and
    the value is the corresponding :class:`MetricsSummaryRow`.  The response
    is intentionally flat so the frontend can look up any window by slug
    without iterating a list.
    """

    windows: Dict[str, MetricsSummaryRow] = Field(
        default_factory=dict,
        description="Map of window slug → pre-computed summary row.",
    )
    namespace: Optional[str] = Field(
        None,
        description="The namespace filter applied, or None for cluster-wide.",
    )


class DashboardTimeseriesResponse(BaseModel):
    """Pre-computed time-series chart data for the frontend dashboard.

    ``points`` is an ordered list of buckets for the requested window,
    ready to be passed directly to the chart builders.
    """

    window_slug: str = Field(..., description="The requested time window slug.")
    namespace: Optional[str] = Field(None, description="Namespace filter applied, or None.")
    points: List[TimeseriesCachePoint] = Field(
        default_factory=list,
        description="Ordered time-series buckets for this window.",
    )
