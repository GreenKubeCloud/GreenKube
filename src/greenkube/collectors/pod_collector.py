# src/greenkube/collectors/pod_collector.py
"""
Collects resource requests and limits (CPU, memory) for every steady-state
container of every pod (including native sidecars) from the Kubernetes API.
"""

import logging
from typing import Iterator, List, Optional

from greenkube.collectors.base_collector import BaseCollector
from greenkube.collectors.k8s_errors import KubernetesListError, list_all_pages
from greenkube.core.config import config as global_config
from greenkube.core.k8s_client import get_apps_v1_api, get_batch_v1_api, get_core_v1_api
from greenkube.core.optimization.targeting import MIRROR_POD_ANNOTATION, STATIC_POD_OWNER_KIND
from greenkube.models.metrics import PodMetric

from ..utils.k8s_utils import parse_cpu_request, parse_memory_request, parse_storage_request

logger = logging.getLogger(__name__)


def _containers_with_kind(spec, include_init_containers: bool) -> Iterator[tuple[object, str]]:
    """Yield ``(container, kind)`` for steady-state containers, then init containers."""
    for container in spec.containers or []:
        yield container, "container"
    for container in getattr(spec, "init_containers", None) or []:
        if getattr(container, "restart_policy", None) == "Always":
            yield container, "sidecar"
        elif include_init_containers:
            yield container, "init"


#: Extended resources of GPU device plugins (NRG-13).
_GPU_RESOURCES = ("nvidia.com/gpu", "amd.com/gpu")
_GPU_RESOURCE_PREFIX = "gpu.intel.com/"


def _gpu_quantity(resources: dict) -> int:
    """Number of GPUs in a requests or limits mapping."""
    total = 0
    for resource, quantity in resources.items():
        if resource in _GPU_RESOURCES or str(resource).startswith(_GPU_RESOURCE_PREFIX):
            try:
                total += int(float(str(quantity)))
            except ValueError:
                continue
    return total


def _container_metric(
    container,
    *,
    kind: str,
    pod_name: str,
    namespace: str,
    owner_kind: Optional[str],
    owner_name: Optional[str],
    pod_uid: Optional[str] = None,
) -> PodMetric:
    resources = getattr(container, "resources", None)
    requests = (getattr(resources, "requests", None) or {}) if resources else {}
    limits = (getattr(resources, "limits", None) or {}) if resources else {}
    return PodMetric(
        pod_name=pod_name,
        namespace=namespace,
        container_name=container.name,
        cpu_request=parse_cpu_request(requests.get("cpu")),
        memory_request=parse_memory_request(requests.get("memory")),
        ephemeral_storage_request=parse_storage_request(requests.get("ephemeral-storage")),
        cpu_limit=parse_cpu_request(limits["cpu"]) if limits.get("cpu") else None,
        memory_limit=parse_memory_request(limits["memory"]) if limits.get("memory") else None,
        gpu_request=_gpu_quantity(requests) or _gpu_quantity(limits),
        kind=kind,
        owner_kind=owner_kind,
        owner_name=owner_name,
        pod_uid=pod_uid,
    )


#: ``(namespace, name) -> (controller kind, controller name)`` of ReplicaSets or Jobs; ``None`` when no controller.
OwnerMap = dict[tuple[str, str], Optional[tuple[str, str]]]


def _controller_of(obj) -> Optional[tuple[str, str]]:
    """The controller ownerReference ``(kind, name)`` of a Kubernetes object, or ``None``."""
    for ref in getattr(obj.metadata, "owner_references", None) or []:
        if getattr(ref, "controller", False) and ref.kind and ref.name:
            return str(ref.kind), str(ref.name)
    return None


def resolve_workload_owner(
    namespace: str,
    owner_kind: Optional[str],
    owner_name: Optional[str],
    replica_sets: Optional[OwnerMap],
    jobs: Optional[OwnerMap],
) -> tuple[Optional[str], Optional[str]]:
    """The workload that owns a pod, following the controller chain (COL-16).

    A ReplicaSet resolves to its controller (Deployment, Argo Rollout, ...) or
    stays a ReplicaSet when it has none; a Job created by a CronJob resolves to
    the CronJob, so every run of it is one workload.  When the ReplicaSet list
    is unavailable (``replica_sets`` is ``None``) or does not hold the
    ReplicaSet, the name rule ``<deployment>-<pod-template-hash>`` is kept so
    Deployment workloads stay stable; the caller logs that degradation.
    """
    if owner_kind == "ReplicaSet" and owner_name:
        if replica_sets is not None and (namespace, owner_name) in replica_sets:
            controller = replica_sets[(namespace, owner_name)]
            return controller if controller is not None else (owner_kind, owner_name)
        parts = owner_name.rsplit("-", 1)
        if len(parts) == 2:
            return "Deployment", parts[0]
        return owner_kind, owner_name
    if owner_kind == "Job" and owner_name and jobs is not None:
        controller = jobs.get((namespace, owner_name))
        if controller is not None and controller[0] == "CronJob":
            return controller
    return owner_kind, owner_name


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

    async def _owner_map(self, kind: str) -> Optional[OwnerMap]:
        """ReplicaSet or Job controllers of the cluster, or ``None`` when they cannot be listed (COL-16)."""
        try:
            if kind == "ReplicaSet":
                api = await get_apps_v1_api()
                call = getattr(api, "list_replica_set_for_all_namespaces", None) if api else None
            else:
                api = await get_batch_v1_api()
                call = getattr(api, "list_job_for_all_namespaces", None) if api else None
            if call is None:
                return None
            items = await list_all_pages(call, watch=False, _request_timeout=global_config.K8S_REQUEST_TIMEOUT or None)
        except Exception as error:  # missing RBAC, API error: owners fall back, visibly
            logger.warning(
                "%s owners unavailable (%s): pod owners use the %s.",
                kind,
                error,
                "name rule <deployment>-<hash>" if kind == "ReplicaSet" else "Job itself",
            )
            return None
        return {(str(item.metadata.namespace), str(item.metadata.name)): _controller_of(item) for item in items}

    async def collect(self, include_init_containers: bool = False, *, raise_on_error: bool = False) -> List[PodMetric]:
        """
        Fetches all pods and extracts their container resource requests and limits.

        Steady-state containers are ``spec.containers`` (``kind="container"``)
        and native sidecars — init containers with ``restartPolicy: Always``
        (``kind="sidecar"``). Classic init containers only run before the pod
        starts, so they are excluded from steady-state sums and returned only
        when ``include_init_containers`` is true (``kind="init"``).

        A failed pod list returns an empty list, or raises
        :class:`KubernetesListError` when ``raise_on_error`` is true so callers
        that store history can tell an API error from an empty cluster (COL-07).
        """
        pod_metrics: List[PodMetric] = []
        api = await self._ensure_client()

        # If there's no configured Kubernetes client, return empty list.
        if not api:
            logger.debug("Kubernetes client not configured; skipping pod collection.")
            return pod_metrics

        try:
            pods = await list_all_pages(
                api.list_pod_for_all_namespaces,
                watch=False,
                _request_timeout=global_config.K8S_REQUEST_TIMEOUT or None,
            )

            owner_kinds = {
                ref.kind
                for pod in pods
                for ref in (getattr(pod.metadata, "owner_references", None) or [])
                if getattr(ref, "controller", False)
            }
            replica_sets = await self._owner_map("ReplicaSet") if "ReplicaSet" in owner_kinds else {}
            jobs = await self._owner_map("Job") if "Job" in owner_kinds else {}

            for pod in pods:
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
                    # The workload is the end of the controller chain (COL-16).
                    owner_kind, owner_name = resolve_workload_owner(
                        namespace, owner_kind, owner_name, replica_sets, jobs
                    )

                annotations = getattr(pod.metadata, "annotations", None) or {}
                if owner_kind is None and isinstance(annotations, dict) and MIRROR_POD_ANNOTATION in annotations:
                    # A static pod's mirror (older kubelets set no Node owner): attribute it to its
                    # node, so it is recognized as a static pod and never advised on (F-04).
                    node_name = getattr(pod.spec, "node_name", None)
                    if node_name:
                        owner_kind, owner_name = STATIC_POD_OWNER_KIND, node_name

                uid = getattr(pod.metadata, "uid", None)
                pod_uid = uid if isinstance(uid, str) and uid else None
                for container, kind in _containers_with_kind(pod.spec, include_init_containers):
                    pod_metrics.append(
                        _container_metric(
                            container,
                            kind=kind,
                            pod_name=pod_name,
                            namespace=namespace,
                            owner_kind=owner_kind,
                            owner_name=owner_name,
                            pod_uid=pod_uid,
                        )
                    )
        except Exception as e:
            logger.exception("Error collecting pod metrics from Kubernetes API: %s", e)
            if raise_on_error:
                raise KubernetesListError(f"Kubernetes pod list failed: {e}") from e
            return []

        logger.debug("Collected %s pod/container request metrics.", len(pod_metrics))
        return pod_metrics

    async def close(self):
        """Close the Kubernetes API client if it exists."""
        if self._api:
            try:
                api_client = getattr(self._api, "api_client", None)
                if api_client and not getattr(api_client, "_is_shared_k8s_client", False):
                    await api_client.close()
                    logger.debug("PodCollector Kubernetes client closed.")
            except Exception:
                logger.exception("Error closing PodCollector Kubernetes client.")
            finally:
                self._api = None
