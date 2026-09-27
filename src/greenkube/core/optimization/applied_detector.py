# src/greenkube/core/optimization/applied_detector.py
"""Detects applied recommendations from the live cluster state (Phase 3).

Merge detection uses the Kubernetes API only for the first iteration: when a
live workload request decreases (or matches the recommendation within the
configured tolerance) and an active recommendation existed for that target, the
recommendation transitions to ``applied``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional, Protocol, Tuple

from greenkube.models.metrics import (
    ApplicationMethod,
    RecommendationRecord,
    RecommendationStatus,
    RecommendationType,
)

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.core.optimization.lifecycle import RecommendationLifecycle

logger = logging.getLogger(__name__)

RIGHTSIZING_TYPES = {
    RecommendationType.RIGHTSIZING_CPU,
    RecommendationType.RIGHTSIZING_MEMORY,
}

_APPS_READ_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"}


@dataclass
class WorkloadSnapshot:
    """Live resource requests for a workload owner."""

    namespace: str
    kind: str
    name: str
    cpu_request_millicores: int = 0
    memory_request_bytes: int = 0


class WorkloadReader(Protocol):
    """Minimal reader contract so tests can inject fakes."""

    async def read(self) -> Dict[Tuple[str, str, str], WorkloadSnapshot]: ...


def _sum_requests(containers) -> Tuple[int, int]:
    cpu = 0
    memory = 0
    for container in containers or []:
        resources = getattr(container, "resources", None)
        requests = getattr(resources, "requests", None) if resources else None
        if not requests:
            continue
        cpu += _parse_cpu(requests.get("cpu"))
        memory += _parse_memory(requests.get("memory"))
    return cpu, memory


def _parse_cpu(value) -> int:
    """Parses a Kubernetes CPU quantity into millicores, returning 0 on failure."""
    if value is None:
        return 0
    from greenkube.utils.k8s_quantities import parse_cpu_quantity

    try:
        return parse_cpu_quantity(value) or 0
    except Exception:
        return 0


def _parse_memory(value) -> int:
    """Parses a Kubernetes memory quantity into bytes, returning 0 on failure."""
    if value is None:
        return 0
    from greenkube.utils.k8s_quantities import parse_memory_quantity

    try:
        return parse_memory_quantity(value) or 0
    except Exception:
        return 0


class K8sWorkloadReader:
    """Reads Deployment/StatefulSet/DaemonSet requests via the Kubernetes API."""

    async def read(self) -> Dict[Tuple[str, str, str], WorkloadSnapshot]:
        snapshots: Dict[Tuple[str, str, str], WorkloadSnapshot] = {}
        try:
            from greenkube.core.k8s_client import get_apps_v1_api

            api = await get_apps_v1_api()
        except Exception as exc:
            logger.warning("Apply detection: could not create apps API client: %s", exc)
            return snapshots

        if api is None:
            logger.warning("Apply detection: no Kubernetes configuration available.")
            return snapshots

        readers = (
            ("Deployment", api.list_deployment_for_all_namespaces),
            ("StatefulSet", api.list_stateful_set_for_all_namespaces),
            ("DaemonSet", api.list_daemon_set_for_all_namespaces),
        )
        for kind, reader in readers:
            try:
                result = await reader()
            except Exception as exc:
                logger.warning("Apply detection: could not list %ss: %s", kind, exc)
                continue
            for item in getattr(result, "items", []) or []:
                metadata = getattr(item, "metadata", None)
                spec = getattr(item, "spec", None)
                template = getattr(spec, "template", None) if spec else None
                pod_spec = getattr(template, "spec", None) if template else None
                if not metadata or not pod_spec:
                    continue
                cpu, memory = _sum_requests(getattr(pod_spec, "containers", None))
                snapshots[(metadata.namespace, kind, metadata.name)] = WorkloadSnapshot(
                    namespace=metadata.namespace,
                    kind=kind,
                    name=metadata.name,
                    cpu_request_millicores=cpu,
                    memory_request_bytes=memory,
                )
        return snapshots


class AppliedDetector:
    """Transitions recommendations to ``applied`` when the change has landed."""

    def __init__(
        self,
        lifecycle: "RecommendationLifecycle",
        reader: Optional[WorkloadReader] = None,
        config: Optional["Config"] = None,
    ):
        from greenkube.core.config import get_config

        self.lifecycle = lifecycle
        self.reader = reader if reader is not None else K8sWorkloadReader()
        self.config = config if config is not None else get_config()

    def _target_key(self, record: RecommendationRecord) -> Optional[Tuple[str, str, str]]:
        namespace = record.namespace
        if not namespace:
            return None
        if record.owner_kind and record.owner_name:
            return (namespace, record.owner_kind, record.owner_name)
        if record.pod_name:
            return (namespace, "Pod", record.pod_name)
        return None

    @staticmethod
    def _decreased(
        observed: Optional[int], current: Optional[int], recommended: Optional[int], tolerance: float
    ) -> bool:
        """Returns True when the live value shows the change landed.

        A change is considered applied when the live request dropped by more
        than the tolerance since the recommendation was generated, or when it
        is within tolerance of the recommended value.
        """
        # A missing request (0) is not an applied change: it means the workload
        # declares no requests, not that the recommendation was implemented.
        if observed is None or observed <= 0 or current is None or current <= 0:
            return False
        if observed <= current * (1 - tolerance):
            return True
        if recommended is not None and recommended > 0:
            return abs(observed - recommended) <= recommended * tolerance
        return False

    async def detect(self) -> List[RecommendationRecord]:
        """Detects and records applied rightsizing recommendations."""
        records = await self.lifecycle.repo.get_recommendations_by_statuses(["active", "pr_open"])
        candidates = [r for r in records if r.type in RIGHTSIZING_TYPES]
        if not candidates:
            return []

        snapshots = await self.reader.read()
        if not snapshots:
            return []

        tolerance = float(getattr(self.config, "RECOMMENDATION_APPLY_TOLERANCE", 0.25))
        applied: List[RecommendationRecord] = []

        for record in candidates:
            key = self._target_key(record)
            if key is None:
                continue
            snapshot = snapshots.get(key)
            if snapshot is None:
                continue

            if record.type == RecommendationType.RIGHTSIZING_CPU:
                observed = snapshot.cpu_request_millicores
                current = record.current_cpu_request_millicores
                recommended = record.recommended_cpu_request_millicores
                actual_cpu: Optional[int] = observed
                actual_memory: Optional[int] = None
            else:
                observed = snapshot.memory_request_bytes
                current = record.current_memory_request_bytes
                recommended = record.recommended_memory_request_bytes
                actual_cpu = None
                actual_memory = observed

            if record.id is None or not self._decreased(observed, current, recommended, tolerance):
                continue

            method = (
                ApplicationMethod.PR_MERGE.value
                if record.status == RecommendationStatus.PR_OPEN
                else ApplicationMethod.DETECTED.value
            )
            try:
                updated = await self.lifecycle.apply(
                    record.id,
                    actual_cpu=actual_cpu,
                    actual_memory=actual_memory,
                    application_method=method,
                    actor="detector",
                )
                applied.append(updated)
                logger.info(
                    "Apply detected for recommendation %d (%s/%s): %s -> %s.",
                    record.id,
                    record.namespace,
                    record.pod_name,
                    current,
                    observed,
                )
            except Exception as exc:
                logger.warning("Apply detection failed for recommendation %s: %s", record.id, exc)
        return applied
