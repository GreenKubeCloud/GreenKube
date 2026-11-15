"""Pod metadata history (WP-01.1, COL-01).

The range processor collects a window after it ended, when a pod of that
window may already be deleted.  Every collection tick therefore records what
the Kubernetes API says about each live pod (owner, requests, limits and
container list) as one version per distinct spec with the interval it was
observed in.  Later and repeated collections resolve the metadata of a sample
from this history instead of from the pods that happen to exist at collection
time.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

from greenkube.models.metrics import PodMetric

#: PodMetric fields stored per container in ``pod_snapshots.containers``.
CONTAINER_FIELDS: tuple[str, ...] = (
    "container_name",
    "kind",
    "cpu_request",
    "memory_request",
    "ephemeral_storage_request",
    "cpu_limit",
    "memory_limit",
)


@dataclass(frozen=True)
class PodSnapshot:
    """One observed version of a pod's metadata."""

    namespace: str
    pod_name: str
    owner_kind: str | None
    owner_name: str | None
    containers: tuple[dict[str, Any], ...]
    first_seen: datetime
    last_seen: datetime
    spec_hash: str = field(default="", compare=False)

    def pod_metrics(self) -> list[PodMetric]:
        """The containers of this version as PodMetric records (the pod collector's shape)."""
        return [
            PodMetric.model_validate(
                {
                    **{key: container.get(key) for key in CONTAINER_FIELDS if container.get(key) is not None},
                    "pod_name": self.pod_name,
                    "namespace": self.namespace,
                    "owner_kind": self.owner_kind,
                    "owner_name": self.owner_name,
                }
            )
            for container in self.containers
        ]


def snapshots_from_pod_metrics(pods: Iterable[PodMetric], seen_at: datetime) -> list[PodSnapshot]:
    """Group the pod collector's per-container records into one snapshot per pod."""
    by_pod: dict[tuple[str, str], list[PodMetric]] = defaultdict(list)
    for pod in pods:
        by_pod[(pod.namespace, pod.pod_name)].append(pod)
    snapshots = []
    for (namespace, pod_name), containers in by_pod.items():
        owner = next(((c.owner_kind, c.owner_name) for c in containers if c.owner_kind and c.owner_name), (None, None))
        payload = tuple(
            sorted(
                ({key: getattr(c, key) for key in CONTAINER_FIELDS} for c in containers),
                key=lambda item: (str(item["kind"]), str(item["container_name"])),
            )
        )
        digest = hashlib.sha256(
            json.dumps({"owner": owner, "containers": payload}, sort_keys=True).encode("utf-8")
        ).hexdigest()
        snapshots.append(
            PodSnapshot(
                namespace=namespace,
                pod_name=pod_name,
                owner_kind=owner[0],
                owner_name=owner[1],
                containers=payload,
                first_seen=seen_at,
                last_seen=seen_at,
                spec_hash=digest,
            )
        )
    return snapshots


def snapshot_at(versions: Sequence[PodSnapshot], timestamp: datetime) -> PodSnapshot | None:
    """The version of a pod valid at ``timestamp``.

    The latest version first observed at or before ``timestamp``; failing that
    the earliest version (the pod was first observed after the sample).
    ``versions`` must be sorted by ``first_seen``.
    """
    valid = None
    for version in versions:
        if version.first_seen <= timestamp:
            valid = version
        else:
            break
    if valid is not None:
        return valid
    return versions[0] if versions else None


class PodSnapshotRepository(ABC):
    """Stores and reads the pod metadata history."""

    @abstractmethod
    async def save_pods(self, pods: Sequence[PodMetric], seen_at: datetime) -> int:
        """Record the live pod list observed at ``seen_at``; returns the number of pods recorded.

        A pod whose owner, requests, limits and containers are unchanged extends
        its current version (``last_seen``); a changed spec starts a new version.
        """

    @abstractmethod
    async def get_snapshots(self, start: datetime, end: datetime) -> dict[tuple[str, str], list[PodSnapshot]]:
        """Versions observed during ``[start, end]``, keyed by ``(namespace, pod)`` and sorted by first_seen."""
