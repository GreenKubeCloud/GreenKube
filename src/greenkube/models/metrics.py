# src/greenkube/models/metrics.py
"""
This module defines the Pydantic data models for all metrics collected and
calculated within the GreenKube application. These models serve as the
single source of truth for our data structures, ensuring type safety and
consistency across all modules (collectors, calculators, reporters).
"""
from pydantic import BaseModel, Field
from datetime import datetime, timezone
from typing import Optional

class EnergyMetric(BaseModel):
    """
    Represents a single energy consumption data point, typically collected
    from a source like Kepler. Now includes metadata for granular calculations.
    """
    pod_name: str = Field(..., description="The name of the Kubernetes pod.")
    namespace: str = Field(..., description="The namespace the pod belongs to.")
    joules: float = Field(..., description="The energy consumed by the pod in Joules over a period.")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="The timestamp of the measurement.")
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
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="The timestamp of the cost calculation.")

class CarbonEmissionMetric(BaseModel):
    """
    Represents a calculated carbon emission data point. This is not directly
    collected but is derived from EnergyMetrics.
    """
    pod_name: str = Field(..., description="The name of the Kubernetes pod.")
    namespace: str = Field(..., description="The namespace the pod belongs to.")
    co2e_grams: float = Field(..., description="The calculated carbon emissions in grams of CO2 equivalent.")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="The timestamp of the calculation.")

class CombinedMetric(BaseModel):
    """
    A combined data model for reporting, linking cost and carbon data for a pod.
    """
    pod_name: str
    namespace: str
    total_cost: float
    co2e_grams: float
    pue: float
    grid_intensity: float

class EnvironmentalMetric(BaseModel):
    """
    Holds environmental factors for a specific location (e.g., a cloud region).
    """
    pue: float = Field(..., description="Power Usage Effectiveness of the data center.")
    grid_intensity: float = Field(..., description="Carbon intensity of the grid in gCO2e/kWh.")