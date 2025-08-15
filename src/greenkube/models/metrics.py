# src/greenkube/models/metrics.py
"""
This module defines the Pydantic data models for all metrics collected and
calculated within the GreenKube application. These models serve as the
single source of truth for our data structures, ensuring type safety and
consistency across all modules (collectors, calculators, reporters).
"""
from pydantic import BaseModel, Field
from datetime import datetime
from datetime import timezone
from typing import List

class EnergyMetric(BaseModel):
    """
    Represents a single energy consumption data point, typically collected
    from a source like Kepler.
    """
    pod_name: str = Field(..., description="The name of the Kubernetes pod.")
    namespace: str = Field(..., description="The namespace the pod belongs to.")
    joules: float = Field(..., description="The energy consumed by the pod in Joules over a period.")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="The timestamp of the measurement.")

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

