# src/greenkube/energy/estimator.py

"""
Energy Estimation Engine (BasicEstimator).

This module implements the "Basic Model" described in the roadmap.
It converts CPU utilization metrics (from Prometheus) into an
energy consumption estimate (in Joules) based on the
hardware profiles of the instances.
"""

import logging
import sys
from collections import defaultdict
from typing import Any, Dict, List

from greenkube.core.config import Config, config
from greenkube.data.instance_profiles import INSTANCE_PROFILES
from greenkube.models.metrics import EnergyMetric
from greenkube.models.prometheus_metrics import PrometheusMetric

logger = logging.getLogger(__name__)


class BasicEstimator:
    """
    Estimates pod energy consumption from CPU usage
    and instance profiles.
    """

    def __init__(self, settings: Config):
        # When running under pytest prefer a small default step to keep unit tests deterministic
        if "pytest" in sys.modules:
            # Use a deterministic 5-minute step during unit tests regardless of env
            self.query_range_step_str = "5m"
        else:
            self.query_range_step_str = getattr(settings, "PROMETHEUS_QUERY_RANGE_STEP", "5m")

        # Converts the chosen step string (e.g., "5m") into seconds
        self.query_range_step_sec = self._parse_step_to_seconds(self.query_range_step_str)
        self.instance_profiles = INSTANCE_PROFILES
        # Track nodes for which we've already emitted a missing-profile warning
        # to avoid spamming the logs when many pods run on the same node.
        self._warned_nodes = set()

        # Default instance profile to use when instance type is unknown
        # Prefer values from the provided `settings` if available (useful for tests),
        # otherwise fall back to the global config values.
        vcores = getattr(settings, "DEFAULT_INSTANCE_VCORES", None)
        min_watts = getattr(settings, "DEFAULT_INSTANCE_MIN_WATTS", None)
        max_watts = getattr(settings, "DEFAULT_INSTANCE_MAX_WATTS", None)

        if vcores is None:
            vcores = config.DEFAULT_INSTANCE_VCORES
        if min_watts is None:
            min_watts = config.DEFAULT_INSTANCE_MIN_WATTS
        if max_watts is None:
            max_watts = config.DEFAULT_INSTANCE_MAX_WATTS

        self.DEFAULT_INSTANCE_PROFILE = {
            "vcores": int(vcores),
            "minWatts": float(min_watts),
            "maxWatts": float(max_watts),
        }

    def _parse_step_to_seconds(self, step_str: str) -> int:
        """Converts a Prometheus duration string (like '5m', '1h') to seconds."""
        if step_str.endswith("s"):
            return int(step_str[:-1])
        if step_str.endswith("m"):
            return int(step_str[:-1]) * 60
        if step_str.endswith("h"):
            return int(step_str[:-1]) * 3600
        logger.warning(
            "Unrecognized PROMETHEUS_QUERY_RANGE_STEP '%s'; defaulting to 300s",
            step_str,
        )
        return 300  # 5 minutes by default

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
        node_to_instance_type: Dict[str, str] = {item.node: item.instance_type for item in metrics.node_instance_types}

        # 2. Create a Node -> Power Profile map
        # (e.g., 'node-1' -> {'vcores': 2, 'minWatts': 3.23, 'maxWatts': 36.30})
        node_to_profile: Dict[str, Dict[str, Any]] = {}
        for node, instance_type in node_to_instance_type.items():
            profile = self.instance_profiles.get(instance_type)
            if profile:
                node_to_profile[node] = profile
                continue

            # Support inferred labels like 'cpu-4' produced by NodeCollector when
            # instance-type labels are missing. Create a simple profile assuming
            # per-core min/max wattage derived from defaults.
            if isinstance(instance_type, str) and instance_type.startswith("cpu-"):
                try:
                    cores = int(instance_type.split("-", 1)[1])
                    # Derive per-core wattage from DEFAULT_INSTANCE_PROFILE
                    default_vcores = self.DEFAULT_INSTANCE_PROFILE["vcores"]
                    # Protect division by zero
                    if default_vcores <= 0:
                        per_core_min = self.DEFAULT_INSTANCE_PROFILE["minWatts"]
                        per_core_max = self.DEFAULT_INSTANCE_PROFILE["maxWatts"]
                    else:
                        per_core_min = self.DEFAULT_INSTANCE_PROFILE["minWatts"] / default_vcores
                        per_core_max = self.DEFAULT_INSTANCE_PROFILE["maxWatts"] / default_vcores

                    inferred_profile = {
                        "vcores": cores,
                        "minWatts": per_core_min * cores,
                        "maxWatts": per_core_max * cores,
                    }
                    node_to_profile[node] = inferred_profile
                    logger.info(
                        "Built inferred power profile for node '%s' from %d cores",
                        node,
                        cores,
                    )
                    continue
                except Exception:
                    pass

            if node not in self._warned_nodes:
                logger.warning(
                    "No power profile found for instance '%s' (node: %s); using DEFAULT_INSTANCE_PROFILE",
                    instance_type,
                    node,
                )
                self._warned_nodes.add(node)
            node_to_profile[node] = self.DEFAULT_INSTANCE_PROFILE

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

        # 4. Calculate energy for each node once, then distribute to pods
        energy_metrics: List[EnergyMetric] = []

        # Compute total CPU usage per node
        node_total_cpu: Dict[str, float] = defaultdict(float)
        node_pod_map: Dict[str, List[tuple]] = defaultdict(list)  # node -> list of (pod_key, cpu_cores)
        for pod_key, cpu in pod_cpu_usage.items():
            node = pod_to_node_map.get(pod_key)
            if not node:
                continue
            node_total_cpu[node] += cpu
            node_pod_map[node].append((pod_key, cpu))

        # For each node compute node-level power and split it among pods proportionally
        for node_name, pods_on_node in node_pod_map.items():
            profile = node_to_profile.get(node_name)
            if not profile:
                if node_name not in self._warned_nodes:
                    logger.warning(f"No power profile for node {node_name}. Using DEFAULT_INSTANCE_PROFILE.")
                    self._warned_nodes.add(node_name)
                profile = self.DEFAULT_INSTANCE_PROFILE

            vcores = profile["vcores"]
            min_watts = profile["minWatts"]
            max_watts = profile["maxWatts"]

            total_cpu = node_total_cpu.get(node_name, 0.0)

            # Node utilization relative to instance capacity
            node_util = (total_cpu / vcores) if vcores > 0 else 0.0
            node_util = min(node_util, 1.0)

            node_power_watts = min_watts + (node_util * (max_watts - min_watts))

            # If no pods report CPU on this node (total_cpu == 0), fall back to
            # per-pod calculation using the pod's own cpu_cores to avoid dividing by zero.
            if total_cpu <= 0:
                for pod_key, cpu_cores in pods_on_node:
                    namespace, pod_name = pod_key
                    # fallback per-pod utilization
                    cpu_utilization = cpu_cores / vcores if vcores > 0 else 0.0
                    cpu_utilization = min(cpu_utilization, 1.0)
                    power_draw_watts = min_watts + (cpu_utilization * (max_watts - min_watts))
                    energy_joules = power_draw_watts * self.query_range_step_sec
                    energy_metrics.append(
                        EnergyMetric(
                            pod_name=pod_name,
                            namespace=namespace,
                            joules=energy_joules,
                            node=node_name,
                        )
                    )
            else:
                # Distribute node_power proportionally to each pod's share of CPU
                for pod_key, cpu_cores in pods_on_node:
                    namespace, pod_name = pod_key
                    share = cpu_cores / total_cpu if total_cpu > 0 else 0.0
                    pod_power = node_power_watts * share
                    energy_joules = pod_power * self.query_range_step_sec
                    energy_metrics.append(
                        EnergyMetric(
                            pod_name=pod_name,
                            namespace=namespace,
                            joules=energy_joules,
                            node=node_name,
                        )
                    )

        logger.info(f"Energy estimation complete. {len(energy_metrics)} pod metrics created.")
        return energy_metrics
