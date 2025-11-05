# src/greenkube/models/metrics.py
"""
This module defines the Pydantic data models for all metrics collected and
calculated within the GreenKube application. These models serve as the
single source of truth for our data structures, ensuring type safety and
consistency across all modules (collectors, calculators, reporters).
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


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


class RecommendationType(str, Enum):
    """Enumeration of possible recommendation types."""

    ZOMBIE_POD = "ZOMBIE_POD"
    RIGHTSIZING_CPU = "RIGHTSIZING_CPU"
    RIGHTSIZING_MEMORY = "RIGHTSIZING_MEMORY"  # Placeholder for future implementation


class Recommendation(BaseModel):
    """Represents a single actionable optimization recommendation."""

    pod_name: str = Field(..., description="The name of the target pod.")
    namespace: str = Field(..., description="The namespace of the target pod.")
    type: RecommendationType = Field(..., description="The category of the recommendation.")
    description: str = Field(..., description="A human-readable description of the recommendation.")


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
    co2e_grams: float = 0.0

    # From EnvironmentalMetric
    pue: float = 1.0
    grid_intensity: float = 0.0

    # From EnergyMetric
    joules: float = 0.0

    # From PodMetric (Note: This may be an aggregation of containers)
    cpu_request: int = 0  # in millicores
    memory_request: int = 0  # in bytes
    # Optional aggregation period (e.g., '2025-11' or '2025')
    period: Optional[str] = None


class EnvironmentalMetric(BaseModel):
    """
    Holds environmental factors for a specific location (e.g., a cloud region).
    """

    pue: float = Field(..., description="Power Usage Effectiveness of the data center.")
    grid_intensity: float = Field(..., description="Carbon intensity of the grid in gCO2e/kWh.")
