"""Collects per-container usage from the Kubernetes Metrics API."""

import logging
from datetime import datetime
from typing import Any

from kubernetes_asyncio.client.rest import ApiException

from greenkube.core.k8s_client import get_custom_objects_api
from greenkube.models.prometheus_metrics import ContainerResourceUsage
from greenkube.utils.k8s_quantities import parse_cpu_quantity, parse_memory_quantity

logger = logging.getLogger(__name__)


def _parse_timestamp(value: Any, *, namespace: str, pod_name: str) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("PodMetrics returned an invalid timestamp for %s/%s.", namespace, pod_name)
        return None


def _parse_duration_seconds(value: Any, *, namespace: str, pod_name: str) -> int | None:
    if not isinstance(value, str) or not value.endswith("s"):
        return None
    try:
        seconds = int(float(value[:-1]))
    except ValueError:
        logger.warning("PodMetrics returned an invalid sample window for %s/%s.", namespace, pod_name)
        return None
    return seconds if seconds > 0 else None


class MetricsServerCollector:
    """Reads read-only ``metrics.k8s.io/v1beta1/pods`` resource usage."""

    def __init__(self, api=None):
        self._api = api

    async def collect(self) -> list[ContainerResourceUsage]:
        """Return currently available CPU and memory samples per running container."""
        if self._api is None:
            self._api = await get_custom_objects_api()
        if self._api is None:
            logger.warning("Kubernetes client unavailable; per-container PodMetrics collection is disabled.")
            return []

        try:
            response = await self._api.list_cluster_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                plural="pods",
            )
        except ApiException as exc:
            logger.warning(
                "Could not read Kubernetes PodMetrics (status=%s, reason=%s); using other resource sources.",
                exc.status,
                exc.reason,
            )
            return []

        items = response.get("items")
        if not isinstance(items, list):
            raise ValueError("Kubernetes PodMetrics response did not contain an items list.")

        metrics: list[ContainerResourceUsage] = []
        for pod in items:
            metadata = pod.get("metadata") or {}
            namespace = metadata.get("namespace")
            pod_name = metadata.get("name")
            if not namespace or not pod_name:
                logger.warning("Skipping a PodMetrics entry without namespace or pod identity.")
                continue
            timestamp = _parse_timestamp(pod.get("timestamp"), namespace=namespace, pod_name=pod_name)
            duration_seconds = _parse_duration_seconds(pod.get("window"), namespace=namespace, pod_name=pod_name)
            containers = pod.get("containers") or []
            for container in containers:
                container_name = container.get("name")
                if not container_name or container_name.strip() != container_name:
                    logger.warning("Skipping PodMetrics for %s/%s with an invalid container name.", namespace, pod_name)
                    continue
                usage = container.get("usage") or {}
                cpu_usage = parse_cpu_quantity(usage.get("cpu"))
                memory_usage = parse_memory_quantity(usage.get("memory"))
                if cpu_usage is None and memory_usage is None:
                    logger.warning(
                        "Skipping PodMetrics for %s/%s/%s without a parseable CPU or memory sample.",
                        namespace,
                        pod_name,
                        container_name,
                    )
                    continue
                metrics.append(
                    ContainerResourceUsage(
                        namespace=namespace,
                        pod_name=pod_name,
                        container_name=container_name,
                        cpu_usage_millicores=cpu_usage,
                        memory_usage_bytes=memory_usage,
                        timestamp=timestamp,
                        duration_seconds=duration_seconds,
                    )
                )

        logger.info("Collected %d current container resource samples from metrics.k8s.io.", len(metrics))
        return metrics
