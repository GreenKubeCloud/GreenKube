# src/greenkube/models/health.py
"""
Pydantic models for collector and service health status.
"""

from datetime import datetime
from enum import Enum
from typing import Dict, Optional

from pydantic import BaseModel, Field


class ServiceStatus(str, Enum):
    """Health status of a service."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"
    UNCONFIGURED = "unconfigured"


class ServiceHealth(BaseModel):
    """Health status of a single service/data source."""

    name: str = Field(..., description="Service name (e.g., 'prometheus', 'opencost').")
    status: ServiceStatus = Field(..., description="Current health status.")
    url: str = Field("", description="Resolved or configured URL for the service.")
    message: str = Field("", description="Human-readable status message.")
    latency_ms: Optional[float] = Field(None, description="Response latency in milliseconds.")
    last_check: Optional[datetime] = Field(None, description="Timestamp of the last health check.")
    configured: bool = Field(False, description="Whether the service URL is explicitly configured.")
    discovered: bool = Field(False, description="Whether the service was discovered via K8s service discovery.")
    inactive: bool = Field(
        False,
        description="Whether the service is present but not the selected provider (excluded from overall status).",
    )


class HealthCheckResponse(BaseModel):
    """Aggregated health check response for all data sources."""

    status: str = Field(..., description="Overall health status ('ok', 'degraded', 'error').")
    version: str = Field(..., description="Application version.")
    services: Dict[str, ServiceHealth] = Field(default_factory=dict, description="Health status per service.")
