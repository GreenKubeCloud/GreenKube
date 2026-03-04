# src/greenkube/core/prometheus_resource_mapper.py
"""Builds per-pod resource maps from PrometheusMetric data."""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Optional

from ..models.prometheus_metrics import PrometheusMetric

logger = logging.getLogger(__name__)


@dataclass
class PodResourceMaps:
    """Per-pod resource usage aggregated from Prometheus data."""

    cpu_usage_map: Dict[tuple, int] = field(default_factory=dict)
    memory_usage_map: Dict[tuple, int] = field(default_factory=dict)
    network_rx_map: Dict[tuple, float] = field(default_factory=dict)
    network_tx_map: Dict[tuple, float] = field(default_factory=dict)
    disk_read_map: Dict[tuple, float] = field(default_factory=dict)
    disk_write_map: Dict[tuple, float] = field(default_factory=dict)
    restart_map: Dict[tuple, int] = field(default_factory=dict)


class PrometheusResourceMapper:
    """Extracts per-pod resource maps from a PrometheusMetric snapshot."""

    @staticmethod
    def build(prom_metrics: Optional[PrometheusMetric]) -> PodResourceMaps:
        """Aggregate all per-pod resource data from Prometheus metrics.

        Returns a PodResourceMaps dataclass with all resource maps.
        """
        maps = PodResourceMaps()
        if not prom_metrics:
            return maps

        # CPU usage
        cpu_agg: Dict[tuple, float] = defaultdict(float)
        for item in prom_metrics.pod_cpu_usage:
            cpu_agg[(item.namespace, item.pod)] += item.cpu_usage_cores
        for key, cores in cpu_agg.items():
            maps.cpu_usage_map[key] = int(round(cores * 1000))

        # Memory usage
        if getattr(prom_metrics, "pod_memory_usage", None):
            mem_agg: Dict[tuple, float] = defaultdict(float)
            for item in prom_metrics.pod_memory_usage:
                mem_agg[(item.namespace, item.pod)] += item.memory_usage_bytes
            for key, mem_bytes in mem_agg.items():
                maps.memory_usage_map[key] = int(round(mem_bytes))

        # Network I/O
        if getattr(prom_metrics, "pod_network_io", None):
            for item in prom_metrics.pod_network_io:
                key = (item.namespace, item.pod)
                maps.network_rx_map[key] = maps.network_rx_map.get(key, 0.0) + item.network_receive_bytes
                maps.network_tx_map[key] = maps.network_tx_map.get(key, 0.0) + item.network_transmit_bytes

        # Disk I/O
        if getattr(prom_metrics, "pod_disk_io", None):
            for item in prom_metrics.pod_disk_io:
                key = (item.namespace, item.pod)
                maps.disk_read_map[key] = maps.disk_read_map.get(key, 0.0) + item.disk_read_bytes
                maps.disk_write_map[key] = maps.disk_write_map.get(key, 0.0) + item.disk_write_bytes

        # Restart counts
        if getattr(prom_metrics, "pod_restart_counts", None):
            for item in prom_metrics.pod_restart_counts:
                key = (item.namespace, item.pod)
                maps.restart_map[key] = maps.restart_map.get(key, 0) + item.restart_count

        return maps
