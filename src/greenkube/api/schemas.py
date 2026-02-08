# src/greenkube/api/schemas.py
"""
Pydantic response schemas for the API.
Keeps API-specific response shapes separate from internal domain models.
"""

from pydantic import BaseModel, Field


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
