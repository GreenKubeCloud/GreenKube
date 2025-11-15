# src/greenkube/collectors/pod_collector.py
"""
Collects resource 'request' data (CPU, memory) for all pods
from the Kubernetes API.
"""

import logging
from typing import List, Optional

from kubernetes import client, config

from greenkube.collectors.base_collector import BaseCollector
from greenkube.models.metrics import PodMetric

logger = logging.getLogger(__name__)


class PodCollector(BaseCollector):
    """
    Connects to the K8s API to find the resource requests
    for every container in every pod.
    """

    def __init__(self):
        try:
            # Try loading in-cluster config first
            config.load_incluster_config()
        except config.ConfigException:
            # Try kubeconfig; if that also fails, warn but still attempt to
            # create the CoreV1Api object. Tests often patch CoreV1Api and
            # expect the collector to use the patched API even when config
            # loading isn't possible in the test environment.
            try:
                config.load_kube_config()
            except config.ConfigException:
                logger.warning("Could not configure Kubernetes client. Neither in-cluster nor local config found.")

        # Attempt to create the API client regardless of config loader outcome.
        try:
            self.v1 = client.CoreV1Api()
            logger.info("PodCollector initialized with Kubernetes client.")
        except Exception as e:
            logger.warning("Failed to create Kubernetes API client: %s", e)
            self.v1 = None
            logger.debug("PodCollector initialized without Kubernetes client.")

    def _parse_cpu_request(self, cpu: Optional[str]) -> int:
        """Converts K8s CPU string to millicores (int)."""
        if not cpu:
            return 0
        if cpu.endswith("m"):
            return int(cpu[:-1])
        if cpu.endswith("n"):
            # Nano-cores are too small to be relevant for this calculation
            return 0
        try:
            # Convert full cores (e.g., "1", "0.5") to millicores
            return int(float(cpu) * 1000)
        except ValueError:
            logger.warning(f"Could not parse CPU request value: {cpu}")
            return 0

    def _parse_memory_request(self, memory: Optional[str]) -> int:
        """Converts K8s memory string to bytes (int)."""
        if not memory:
            return 0

        # Handle binary units (Ki, Mi, Gi, Ti)
        if memory.endswith("Ki"):
            return int(memory[:-2]) * 1024
        if memory.endswith("Mi"):
            return int(memory[:-2]) * 1024**2
        if memory.endswith("Gi"):
            return int(memory[:-2]) * 1024**3
        if memory.endswith("Ti"):
            return int(memory[:-2]) * 1024**4

        # Handle decimal units (K, M, G, T)
        if memory.endswith("K"):
            return int(memory[:-1]) * 1000
        if memory.endswith("M"):
            return int(memory[:-1]) * 1000**2
        if memory.endswith("G"):
            return int(memory[:-1]) * 1000**3
        if memory.endswith("T"):
            return int(memory[:-1]) * 1000**4

        try:
            # Assume plain number is in bytes
            return int(memory)
        except ValueError:
            logger.warning(f"Could not parse memory request value: {memory}")
            return 0

    def collect(self) -> List[PodMetric]:
        """
        Fetches all pods and extracts their container resource requests.
        """
        pod_metrics: List[PodMetric] = []
        # If there's no configured Kubernetes client, return empty list.
        if not getattr(self, "v1", None):
            logger.debug("Kubernetes client not configured; skipping pod collection.")
            return pod_metrics

        try:
            pod_list = self.v1.list_pod_for_all_namespaces(watch=False)

            for pod in pod_list.items:
                pod_name = pod.metadata.name
                namespace = pod.metadata.namespace

                if not pod.spec or not pod.spec.containers:
                    continue

                for container in pod.spec.containers:
                    container_name = container.name
                    requests = container.resources.requests or {}

                    cpu_request_str = requests.get("cpu")
                    memory_request_str = requests.get("memory")

                    cpu_request = self._parse_cpu_request(cpu_request_str)
                    memory_request = self._parse_memory_request(memory_request_str)

                    pod_metrics.append(
                        PodMetric(
                            pod_name=pod_name,
                            namespace=namespace,
                            container_name=container_name,
                            cpu_request=cpu_request,
                            memory_request=memory_request,
                        )
                    )
        except Exception as e:
            logger.error(f"Error collecting pod metrics from Kubernetes API: {e}", exc_info=True)
            return []  # Return empty list on failure

        logger.debug(f"Collected {len(pod_metrics)} pod/container request metrics.")
        return pod_metrics
