# src/greenkube/core/prometheus_resource_mapper.py
"""Builds per-pod resource maps from PrometheusMetric data."""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Sequence

from ..models.prometheus_metrics import ContainerResourceUsage, PrometheusMetric

logger = logging.getLogger(__name__)


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


class PrometheusResourceMapper:
    """Extracts per-pod resource maps from a PrometheusMetric snapshot."""

    @staticmethod
    def build(
        prom_metrics: Optional[PrometheusMetric],
        estimated_metrics: Optional[PrometheusMetric] = None,
        container_usages: Sequence[ContainerResourceUsage] = (),
    ) -> PodResourceMaps:
        """Aggregate all per-pod resource data from Prometheus metrics.

        Returns a PodResourceMaps dataclass with all resource maps.
        """
        maps = PodResourceMaps()
        if not prom_metrics:
            return maps

        # CPU usage
        cpu_agg: Dict[tuple, float] = defaultdict(float)
        container_cpu_agg: Dict[tuple, float] = defaultdict(float)
        for item in prom_metrics.pod_cpu_usage:
            cpu_agg[(item.namespace, item.pod)] += item.cpu_usage_cores
            if item.container and item.container.strip() == item.container:
                container_cpu_agg[(item.namespace, item.pod, item.container)] += item.cpu_usage_cores
        for key, cores in cpu_agg.items():
            usage = int(round(cores * 1000))
            maps.cpu_usage_map[key] = usage
            maps.observed_cpu_usage_map[key] = usage
        maps.container_cpu_usage_map = {key: int(round(cores * 1000)) for key, cores in container_cpu_agg.items()}

        if estimated_metrics:
            estimated_cpu_agg: Dict[tuple, float] = defaultdict(float)
            for item in estimated_metrics.pod_cpu_usage:
                if item.estimated_cpu_usage_cores is not None:
                    estimated_cpu_agg[(item.namespace, item.pod)] += item.estimated_cpu_usage_cores
            for key, cores in estimated_cpu_agg.items():
                maps.estimated_cpu_usage_map[key] = int(round(cores * 1000))

        # Memory usage
        if getattr(prom_metrics, "pod_memory_usage", None):
            mem_agg: Dict[tuple, float] = defaultdict(float)
            container_mem_agg: Dict[tuple, float] = defaultdict(float)
            for item in prom_metrics.pod_memory_usage:
                mem_agg[(item.namespace, item.pod)] += item.memory_usage_bytes
                if item.container and item.container.strip() == item.container:
                    container_mem_agg[(item.namespace, item.pod, item.container)] += item.memory_usage_bytes
            for key, mem_bytes in mem_agg.items():
                maps.memory_usage_map[key] = int(round(mem_bytes))
            maps.container_memory_usage_map = {
                key: int(round(mem_bytes)) for key, mem_bytes in container_mem_agg.items()
            }

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
                if item.container and item.container.strip() == item.container:
                    container_key = (item.namespace, item.pod, item.container)
                    maps.container_restart_map[container_key] = (
                        maps.container_restart_map.get(container_key, 0) + item.restart_count
                    )

        for sample in container_usages:
            key = (sample.namespace, sample.pod_name, sample.container_name)
            usage_added = False
            if sample.cpu_usage_millicores is not None and key not in maps.container_cpu_usage_map:
                maps.container_cpu_usage_map[key] = sample.cpu_usage_millicores
                usage_added = True
            if sample.memory_usage_bytes is not None and key not in maps.container_memory_usage_map:
                maps.container_memory_usage_map[key] = sample.memory_usage_bytes
                usage_added = True
            if usage_added:
                maps.container_sample_count_map[key] = 1
                if sample.timestamp is not None:
                    maps.container_timestamp_map[key] = sample.timestamp
                if sample.duration_seconds is not None:
                    maps.container_duration_seconds_map[key] = sample.duration_seconds

        return maps
