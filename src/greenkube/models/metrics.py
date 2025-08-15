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
    timestamp: datetime = Field(default_factory=datetime.now(timezone.utc), description="The timestamp of the measurement.")


class CarbonEmissionMetric(BaseModel):
    """
    Represents a calculated carbon emission data point. This is not directly
    collected but is derived from EnergyMetrics.
    """
    pod_name: str = Field(..., description="The name of the Kubernetes pod.")
    namespace: str = Field(..., description="The namespace the pod belongs to.")
    co2e_grams: float = Field(..., description="The calculated carbon emissions in grams of CO2 equivalent.")
    timestamp: datetime = Field(default_factory=datetime.now(timezone.utc), description="The timestamp of the calculation.")

