# src/greenkube/energy/estimator.py

"""
Energy Estimation Engine (BasicEstimator).

This module implements the "Basic Model" described in the roadmap.
It converts CPU utilization metrics (from Prometheus) into an
energy consumption estimate (in Joules) based on the
hardware profiles of the instances.
"""

import logging
from typing import List, Dict, Any
from collections import defaultdict

from greenkube.core.config import Config
from greenkube.models.prometheus_metrics import PrometheusMetric
from greenkube.models.metrics import EnergyMetric
from greenkube.data.instance_profiles import INSTANCE_PROFILES

logger = logging.getLogger(__name__)

class BasicEstimator:
    """
    Estimates pod energy consumption from CPU usage
    and instance profiles.
    """
    def __init__(self, settings: Config):
        self.query_range_step_str = settings.PROMETHEUS_QUERY_RANGE_STEP
        # Converts the string (e.g., "5m") into seconds
        self.query_range_step_sec = self._parse_step_to_seconds(settings.PROMETHEUS_QUERY_RANGE_STEP)
        self.instance_profiles = INSTANCE_PROFILES

    def _parse_step_to_seconds(self, step_str: str) -> int:
        """Converts a Prometheus duration string (like '5m', '1h') to seconds."""
        if step_str.endswith('s'):
            return int(step_str[:-1])
        if step_str.endswith('m'):
            return int(step_str[:-1]) * 60
        if step_str.endswith('h'):
            return int(step_str[:-1]) * 3600
        logger.warning(f"Unrecognized 'PROMETHEUS_QUERY_RANGE_STEP' format: {step_str}. Defaulting to 300s.")
        return 300 # 5 minutes by default

    def estimate(self, metrics: PrometheusMetric) -> List[EnergyMetric]:
        """
        Orchestrates the energy estimation for all pods.

        Args:
            metrics: The object containing data from Prometheus.

        Returns:
            A list of EnergyMetric, each representing the estimated
            energy consumption for one pod.
        """
        
        # 1. Create a Node -> InstanceType map
        # (e.g., 'node-1' -> 'm5.large')
        node_to_instance_type: Dict[str, str] = {
            item.node: item.instance_type for item in metrics.node_instance_types
        }

        # 2. Create a Node -> Power Profile map
        # (e.g., 'node-1' -> {'vcores': 2, 'minWatts': 3.23, 'maxWatts': 36.30})
        node_to_profile: Dict[str, Dict[str, Any]] = {}
        for node, instance_type in node_to_instance_type.items():
            profile = self.instance_profiles.get(instance_type)
            if profile:
                node_to_profile[node] = profile
            else:
                logger.warning(f"No power profile found for instance type: {instance_type} (node: {node}). This node will be skipped.")

        # 3. Aggregate CPU usage by Pod
        # Prometheus metrics are per *container*. We aggregate them
        # by pod for this initial estimation.
        pod_cpu_usage: Dict[tuple, float] = defaultdict(float)
        pod_to_node_map: Dict[tuple, str] = {}
        
        for item in metrics.pod_cpu_usage:
            pod_key = (item.namespace, item.pod)
            pod_cpu_usage[pod_key] += item.cpu_usage_cores
            
            # Store the node associated with this pod
            if pod_key not in pod_to_node_map:
                pod_to_node_map[pod_key] = item.node

        # 4. Calculate energy for each pod
        energy_metrics: List[EnergyMetric] = []
        
        for (namespace, pod_name), cpu_cores in pod_cpu_usage.items():
            pod_key = (namespace, pod_name)
            node_name = pod_to_node_map.get(pod_key)

            if not node_name:
                logger.warning(f"No node found for pod {namespace}/{pod_name}. Skipping.")
                continue

            profile = node_to_profile.get(node_name)
            if not profile:
                logger.warning(f"No power profile for node {node_name} (pod: {namespace}/{pod_name}). Skipping.")
                continue
            
            # 5. Apply the estimation formula (Linear Interpolation)
            vcores = profile['vcores']
            min_watts = profile['minWatts']
            max_watts = profile['maxWatts']

            # Calculate the % CPU utilization relative to the instance total
            # Note: This is an approximation. CPU usage is attributed to the pod.
            cpu_utilization = cpu_cores / vcores
            # Cap at 100% in case Prometheus data is strange
            cpu_utilization = min(cpu_utilization, 1.0) 

            # Formula: Power = Idle + %Usage * (Max - Idle)
            power_draw_watts = min_watts + (cpu_utilization * (max_watts - min_watts))

            # 6. Convert Power (Watts) to Energy (Joules)
            # Energy (Joules) = Power (Watts) * Time (Seconds)
            energy_joules = power_draw_watts * self.query_range_step_sec

            energy_metrics.append(
                EnergyMetric(
                    pod_name=pod_name,
                    namespace=namespace,
                    joules=energy_joules,
                    node=node_name
                    # region and timestamp will be added by the DataProcessor later
                )
            )

        logger.info(f"Energy estimation complete. {len(energy_metrics)} pod metrics created.")
        return energy_metrics

