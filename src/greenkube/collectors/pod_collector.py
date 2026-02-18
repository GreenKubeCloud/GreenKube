# src/greenkube/collectors/pod_collector.py
"""
Collects resource 'request' data (CPU, memory) for all pods
from the Kubernetes API.
"""

import logging
from typing import List

from greenkube.collectors.base_collector import BaseCollector
from greenkube.core.k8s_client import get_core_v1_api
from greenkube.models.metrics import PodMetric

from ..utils.k8s_utils import parse_cpu_request, parse_memory_request

logger = logging.getLogger(__name__)


class PodCollector(BaseCollector):
    """
    Connects to the K8s API to find the resource requests
    for every container in every pod.
    """

    def __init__(self):
        self._api = None

    async def _ensure_client(self):
        """Lazily initialize the Kubernetes Client."""
        if self._api:
            return self._api

        self._api = await get_core_v1_api()
        if self._api:
            logger.debug("PodCollector initialized with centralized config.")
        else:
            logger.warning("PodCollector could not initialize Kubernetes client.")

        return self._api

    async def collect(self) -> List[PodMetric]:
        """
        Fetches all pods and extracts their container resource requests.
        """
        pod_metrics: List[PodMetric] = []
        api = await self._ensure_client()

        # If there's no configured Kubernetes client, return empty list.
        if not api:
            logger.debug("Kubernetes client not configured; skipping pod collection.")
            return pod_metrics

        try:
            pod_list = await api.list_pod_for_all_namespaces(watch=False)

            for pod in pod_list.items:
                pod_name = pod.metadata.name
                namespace = pod.metadata.namespace

                if not pod.spec or not pod.spec.containers:
                    continue

                # Extract owner reference (Deployment, StatefulSet, etc.)
                owner_kind = None
                owner_name = None
                if pod.metadata.owner_references:
                    # Pick the first controller owner reference (usually ReplicaSet)
                    for ref in pod.metadata.owner_references:
                        if ref.controller:
                            owner_kind = ref.kind
                            owner_name = ref.name
                            break
                    # If we have a ReplicaSet owner, try to get its parent Deployment name
                    # by stripping the ReplicaSet hash suffix (e.g., "nginx-abc123" -> "nginx")
                    if owner_kind == "ReplicaSet" and owner_name:
                        # ReplicaSet names follow the pattern <deployment-name>-<hash>
                        parts = owner_name.rsplit("-", 1)
                        if len(parts) == 2:
                            owner_kind = "Deployment"
                            owner_name = parts[0]

                for container in pod.spec.containers:
                    container_name = container.name
                    requests = container.resources.requests or {}

                    cpu_request_str = requests.get("cpu")
                    memory_request_str = requests.get("memory")

                    cpu_request = parse_cpu_request(cpu_request_str)
                    memory_request = parse_memory_request(memory_request_str)

                    pod_metrics.append(
                        PodMetric(
                            pod_name=pod_name,
                            namespace=namespace,
                            container_name=container_name,
                            cpu_request=cpu_request,
                            memory_request=memory_request,
                            owner_kind=owner_kind,
                            owner_name=owner_name,
                        )
                    )
        except Exception as e:
            logger.error(f"Error collecting pod metrics from Kubernetes API: {e}", exc_info=True)
            return []  # Return empty list on failure

        logger.debug(f"Collected {len(pod_metrics)} pod/container request metrics.")
        return pod_metrics

    async def close(self):
        """Close the Kubernetes API client if it exists."""
        if self._api:
            await self._api.api_client.close()
            logger.debug("PodCollector Kubernetes client closed.")
            self._api = None
