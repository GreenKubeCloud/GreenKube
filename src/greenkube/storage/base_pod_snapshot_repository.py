"""Pod metadata history (WP-01.1, COL-01).

The range processor collects a window after it ended, when a pod of that
window may already be deleted.  Every collection tick therefore records what
the Kubernetes API says about each live pod (owner, requests, limits and
container list) as one version per distinct spec with the interval it was
observed in.  A version is one observation interval: a spec that changes and
changes back (A, B, A) yields three versions, and a new pod UID under the same
name (a recreated StatefulSet pod) starts a new version.  Later and repeated
collections resolve the metadata of a sample from this history instead of from
the pods that happen to exist at collection time.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from greenkube.models.metrics import PodMetric

#: Owners whose pods get a generated name (a recreated pod has a new name), so a version
#: observed after a sample still describes the pod that ran then.  StatefulSet and bare
#: pods reuse their names.
GENERATED_NAME_OWNERS: frozenset[str | None] = frozenset(
    {"Deployment", "ReplicaSet", "DaemonSet", "Job", "CronJob", "Rollout"}  # CronJob, Rollout: COL-16
)
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
#: Stored only when non-zero, so the spec hash of pods without GPUs is unchanged (NRG-13).
OPTIONAL_CONTAINER_FIELDS: tuple[str, ...] = ("gpu_request",)


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
    pod_uid: str | None = None
    cluster_uid: str | None = None

    def pod_metrics(self) -> list[PodMetric]:
        """The containers of this version as PodMetric records (the pod collector's shape)."""
        return [
            PodMetric.model_validate(
                {
                    **{
                        key: container.get(key)
                        for key in (*CONTAINER_FIELDS, *OPTIONAL_CONTAINER_FIELDS)
                        if container.get(key) is not None
                    },
                    "pod_name": self.pod_name,
                    "namespace": self.namespace,
                    "owner_kind": self.owner_kind,
                    "owner_name": self.owner_name,
                    "pod_uid": self.pod_uid,
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
        pod_uid = next((c.pod_uid for c in containers if c.pod_uid), None)
        payload = tuple(
            sorted(
                (
                    {
                        **{key: getattr(c, key) for key in CONTAINER_FIELDS},
                        **{key: getattr(c, key) for key in OPTIONAL_CONTAINER_FIELDS if getattr(c, key)},
                    }
                    for c in containers
                ),
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
                pod_uid=pod_uid,
            )
        )
    return snapshots


@dataclass(frozen=True)
class LatestVersion:
    """The most recent stored version of a pod, as needed to decide how to record a new observation."""

    version_id: int
    spec_hash: str
    pod_uid: str | None


def continues_version(latest: LatestVersion | None, snapshot: PodSnapshot) -> bool:
    """True when ``snapshot`` extends the pod's latest version instead of starting a new one.

    Same spec and no UID change (an unknown UID on either side is not a
    change).  A spec that differs from the latest version always starts a new
    version, even when an older version had it (rollback A, B, A).
    """
    if latest is None or latest.spec_hash != snapshot.spec_hash:
        return False
    return not (latest.pod_uid and snapshot.pod_uid and latest.pod_uid != snapshot.pod_uid)


def snapshot_uncertain(
    versions: Sequence[PodSnapshot], version: PodSnapshot, timestamp: datetime, tolerance: timedelta
) -> bool:
    """True when ``version`` (the one valid at ``timestamp``) may not describe the pod then (review 6).

    The sample lies in an unobserved gap after ``version`` (later than its
    ``last_seen`` plus ``tolerance``) and the next version is another pod
    incarnation: a different UID, or no UID to tell (the pod may have been
    recreated with a new spec during the gap).  Or the sample precedes the
    pod's first observation by more than ``tolerance`` and its owner reuses
    pod names (StatefulSet, bare pod), see :data:`GENERATED_NAME_OWNERS`.
    """
    if timestamp < version.first_seen - tolerance:
        # Sample before the pod's first observation (first-install backfill,
        # downtime): a name reused by its owner may have had another spec.
        return version.owner_kind not in GENERATED_NAME_OWNERS
    if timestamp <= version.last_seen + tolerance:
        return False
    following = next((candidate for candidate in versions if candidate.first_seen > version.first_seen), None)
    if following is None:
        return False
    return not (version.pod_uid and following.pod_uid and version.pod_uid == following.pod_uid)


def snapshot_at(versions: Sequence[PodSnapshot], timestamp: datetime) -> PodSnapshot | None:
    """The version of a pod valid at ``timestamp``.

    The latest version first observed at or before ``timestamp``; failing that
    the earliest version (the pod was first observed after the sample).
    ``versions`` must be sorted by ``first_seen`` (then insertion order).
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
    async def save_pods(self, pods: Sequence[PodMetric], seen_at: datetime, *, cluster_uid: str | None = None) -> int:
        """Record the live pod list observed at ``seen_at``; returns the number of pods recorded.

        A pod whose owner, requests, limits, containers and UID are unchanged
        since its latest version extends that version (``last_seen``); anything
        else starts a new version (see :func:`continues_version`).
        """

    @abstractmethod
    async def get_snapshots(self, start: datetime, end: datetime) -> dict[tuple[str, str], list[PodSnapshot]]:
        """Versions observed during ``[start, end]``, keyed by ``(namespace, pod)`` and sorted by first_seen."""

    @abstractmethod
    async def get_pod_versions(
        self, pods: Iterable[tuple[str, str]], end: datetime
    ) -> dict[tuple[str, str], list[PodSnapshot]]:
        """Every retained version of the given pods first observed at or before ``end``, without lower bound.

        Used for pods of a window that the range read did not cover, for
        example a pod deleted during a controller downtime (review 2).
        """

    @abstractmethod
    async def prune(self, before: datetime) -> int:
        """Delete versions last observed before ``before``; returns the number deleted."""
