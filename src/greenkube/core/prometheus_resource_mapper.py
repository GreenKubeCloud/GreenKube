# src/greenkube/core/prometheus_resource_mapper.py
"""Per-pod and per-container resource maps used to build metric rows."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict


@dataclass
class PodResourceMaps:
    """Per-pod resource usage aggregated from Prometheus data."""

    cpu_usage_map: Dict[tuple, int] = field(default_factory=dict)
    container_cpu_usage_map: Dict[tuple, int] = field(default_factory=dict)
    observed_cpu_usage_map: Dict[tuple, int] = field(default_factory=dict)
    estimated_cpu_usage_map: Dict[tuple, int] = field(default_factory=dict)
    memory_usage_map: Dict[tuple, int] = field(default_factory=dict)
    container_memory_usage_map: Dict[tuple, int] = field(default_factory=dict)
    container_restart_map: Dict[tuple, int] = field(default_factory=dict)
    container_timestamp_map: Dict[tuple, datetime] = field(default_factory=dict)
    container_duration_seconds_map: Dict[tuple, int] = field(default_factory=dict)
    container_sample_count_map: Dict[tuple, int] = field(default_factory=dict)
    network_rx_map: Dict[tuple, float] = field(default_factory=dict)
    network_tx_map: Dict[tuple, float] = field(default_factory=dict)
    disk_read_map: Dict[tuple, float] = field(default_factory=dict)
    disk_write_map: Dict[tuple, float] = field(default_factory=dict)
    restart_map: Dict[tuple, int] = field(default_factory=dict)
