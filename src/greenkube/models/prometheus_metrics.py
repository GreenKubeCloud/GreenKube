# src/greenkube/models/prometheus.py
"""
Pydantic models for structured data returned from the PrometheusCollector.
"""
from pydantic import BaseModel, Field
from typing import List

class PodCPUUsage(BaseModel):
    """
    Represents the average CPU usage for a single container.
    """
    namespace: str
    pod: str
    container: str
    cpu_usage_cores: float = Field(..., description="Average CPU usage in cores")

class NodeInstanceType(BaseModel):
    """
    Maps a Kubernetes node name to its cloud instance type.
    """
    node: str
    instance_type: str = Field(..., description="Cloud provider instance type (e.g., 'm5.large')")

class PrometheusMetric(BaseModel):
    """
    A container for all metrics fetched from Prometheus by the collector.
    This structured data is the input for the BasicEstimator.
    """
    pod_cpu_usage: List[PodCPUUsage] = Field(default_factory=list)
    node_instance_types: List[NodeInstanceType] = Field(default_factory=list)
