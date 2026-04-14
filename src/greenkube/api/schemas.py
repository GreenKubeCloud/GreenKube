# src/greenkube/api/schemas.py
"""
Pydantic response schemas for the API.
Keeps API-specific response shapes separate from internal domain models.
"""

from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from greenkube.models.metrics import CombinedMetric, MetricsSummaryRow


class HealthResponse(BaseModel):
    """Response schema for the health check endpoint."""

    status: str = Field(..., description="Health status of the API.")
    version: str = Field(..., description="Current application version.")


class VersionResponse(BaseModel):
    """Response schema for the version endpoint."""

    version: str = Field(..., description="Current application version.")


class MetricsSummaryResponse(BaseModel):
    """Aggregated summary of metrics over a time range."""

    total_co2e_grams: float = Field(0.0, description="Total operational CO2e in grams.")
    total_embodied_co2e_grams: float = Field(0.0, description="Total embodied CO2e in grams.")
    total_cost: float = Field(0.0, description="Total cost in dollars.")
    total_energy_joules: float = Field(0.0, description="Total energy in Joules.")
    pod_count: int = Field(0, description="Number of unique pods.")
    namespace_count: int = Field(0, description="Number of unique namespaces.")


class TimeseriesPoint(BaseModel):
    """A single data point in a time-series aggregation."""

    timestamp: str = Field(..., description="The bucket timestamp (ISO 8601 or week format).")
    co2e_grams: float = Field(0.0, description="Total operational CO2e in grams for this bucket.")
    embodied_co2e_grams: float = Field(0.0, description="Total embodied CO2e in grams for this bucket.")
    total_cost: float = Field(0.0, description="Total cost in dollars for this bucket.")
    joules: float = Field(0.0, description="Total energy in Joules for this bucket.")
    pod_count: int = Field(0, description="Number of unique pods in this bucket.")
    namespace_count: int = Field(0, description="Number of unique namespaces in this bucket.")


class ConfigResponse(BaseModel):
    """Non-sensitive configuration values."""

    db_type: str
    cloud_provider: str
    default_zone: str
    default_intensity: float
    default_pue: float
    log_level: str
    normalization_granularity: str
    prometheus_query_range_step: str
    api_host: str
    api_port: int


class PaginatedMetricsResponse(BaseModel):
    """Paginated list of combined metrics."""

    total: int = Field(..., description="Total number of matching records.")
    offset: int = Field(0, description="Number of records skipped.")
    limit: int = Field(1000, description="Maximum records in this page.")
    items: List[CombinedMetric] = Field(default_factory=list, description="The metrics in this page.")


class ReportSummaryResponse(BaseModel):
    """Preview summary for a report before export."""

    total_rows: int = Field(0, description="Total number of rows in the report.")
    total_co2e_grams: float = Field(0.0, description="Total operational CO2e in grams.")
    total_embodied_co2e_grams: float = Field(0.0, description="Total embodied CO2e in grams.")
    total_cost: float = Field(0.0, description="Total cost in dollars.")
    total_energy_joules: float = Field(0.0, description="Total energy in Joules.")
    unique_pods: int = Field(0, description="Number of unique pods.")
    unique_namespaces: int = Field(0, description="Number of unique namespaces.")


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
