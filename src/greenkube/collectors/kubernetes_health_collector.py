"""Collection of Kubernetes workload health signals used by verification."""

import logging
from typing import Optional

from greenkube.core.config import config as global_config
from greenkube.core.k8s_client import get_core_v1_api
from greenkube.models.verification import KubernetesHealthObservation

from .base_collector import BaseCollector

logger = logging.getLogger(__name__)


class KubernetesHealthCollector(BaseCollector):
    """Collects readiness, restarts, and OOM signals from the Kubernetes API."""

    def __init__(self, api=None):
        self._api = api

    async def _ensure_client(self):
        if self._api is None:
            self._api = await get_core_v1_api()
        return self._api

    async def collect(
        self, namespace: Optional[str] = None, workload: Optional[str] = None
    ) -> KubernetesHealthObservation:
        """Return a point-in-time health observation for a namespace/workload."""
        api = await self._ensure_client()
        if api is None:
            logger.warning("Kubernetes client unavailable; health verification is inconclusive.")
            return KubernetesHealthObservation(namespace=namespace, workload=workload)

        try:
            kwargs = {"watch": False, "_request_timeout": global_config.K8S_REQUEST_TIMEOUT or None}
            if namespace:
                pods = await api.list_namespaced_pod(namespace=namespace, **kwargs)
            else:
                pods = await api.list_pod_for_all_namespaces(**kwargs)
        except Exception:
            logger.exception("Could not collect Kubernetes health for namespace %s", namespace)
            return KubernetesHealthObservation(namespace=namespace, workload=workload)

        selected = []
        for pod in getattr(pods, "items", []) or []:
            metadata = getattr(pod, "metadata", None)
            if namespace and getattr(metadata, "namespace", namespace) != namespace:
                continue
            labels = getattr(metadata, "labels", None) or {}
            if workload and labels.get("app.kubernetes.io/name") not in (None, workload):
                continue
            selected.append(pod)

        ready = restarts = oom_kills = unavailable = 0
        for pod in selected:
            statuses = getattr(getattr(pod, "status", None), "container_statuses", None) or []
            pod_ready = bool(statuses and all(bool(getattr(status, "ready", False)) for status in statuses))
            ready += int(pod_ready)
            unavailable += int(not pod_ready)
            for status in statuses:
                restarts += int(getattr(status, "restart_count", 0) or 0)
                terminated = getattr(getattr(status, "last_state", None), "terminated", None)
                oom_kills += int(getattr(terminated, "reason", "") == "OOMKilled")

        return KubernetesHealthObservation(
            namespace=namespace,
            workload=workload,
            ready_pods=ready,
            total_pods=len(selected),
            restart_count=restarts,
            oom_kill_count=oom_kills,
            unavailable_pods=unavailable,
        )

    async def close(self):
        """Close an owned Kubernetes client."""
        api_client = getattr(self._api, "api_client", None)
        if api_client and not getattr(api_client, "_is_shared_k8s_client", False):
            await api_client.close()
        self._api = None
