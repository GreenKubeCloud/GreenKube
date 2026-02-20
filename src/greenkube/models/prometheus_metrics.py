# src/greenkube/models/prometheus_metrics.py
"""
Pydantic models for structured data returned from the PrometheusCollector.
"""

from typing import List

from pydantic import BaseModel, ConfigDict, Field


class PodCPUUsage(BaseModel):
    """
    Represents the average CPU usage for a single container.
    """

    model_config = ConfigDict()

    namespace: str
    pod: str
    container: str
    node: str = Field(..., description="The node on which the pod is running")
    cpu_usage_cores: float = Field(..., description="Average CPU usage in cores")


class PodMemoryUsage(BaseModel):
    """
    Represents the memory working set for a single container/pod.
    """

    model_config = ConfigDict()

    namespace: str
    pod: str
    node: str = Field(..., description="The node on which the pod is running")
    memory_usage_bytes: float = Field(..., description="Memory working set in bytes")


class PodNetworkIO(BaseModel):
    """
    Represents network I/O metrics for a single pod.
    """

    model_config = ConfigDict()

    namespace: str
    pod: str
    node: str = Field(..., description="The node on which the pod is running")
    network_receive_bytes: float = Field(0.0, description="Network bytes received per second")
    network_transmit_bytes: float = Field(0.0, description="Network bytes transmitted per second")


class PodDiskIO(BaseModel):
    """
    Represents disk I/O metrics for a single pod.
    """

    model_config = ConfigDict()

    namespace: str
    pod: str
    node: str = Field(..., description="The node on which the pod is running")
    disk_read_bytes: float = Field(0.0, description="Disk bytes read per second")
    disk_write_bytes: float = Field(0.0, description="Disk bytes written per second")


class PodRestartCount(BaseModel):
    """
    Represents the total restart count for a container in a pod.
    """

    model_config = ConfigDict()

    namespace: str
    pod: str
    container: str = Field("", description="Container name")
    restart_count: int = Field(0, description="Total number of container restarts")


class NodeInstanceType(BaseModel):
    """
    Maps a Kubernetes node name to its cloud instance type.
    """

    model_config = ConfigDict()

    node: str
    instance_type: str = Field(..., description="Cloud provider instance type (e.g., 'm5.large')")


class PrometheusMetric(BaseModel):
    """
    A container for all metrics fetched from Prometheus by the collector.
    This structured data is the input for the BasicEstimator.
    """

    model_config = ConfigDict()

    pod_cpu_usage: List[PodCPUUsage] = Field(default_factory=list)
    pod_memory_usage: List[PodMemoryUsage] = Field(default_factory=list)
    node_instance_types: List[NodeInstanceType] = Field(default_factory=list)
    pod_network_io: List[PodNetworkIO] = Field(default_factory=list)
    pod_disk_io: List[PodDiskIO] = Field(default_factory=list)
    pod_restart_counts: List[PodRestartCount] = Field(default_factory=list)
