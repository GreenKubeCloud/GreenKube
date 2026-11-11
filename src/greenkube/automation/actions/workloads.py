"""Pod-template workload kinds that container-level action kinds can address."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: API versions the planners accept per workload kind.
WORKLOAD_API_VERSIONS: Mapping[str, frozenset[str]] = {
    "CronJob": frozenset({"batch/v1"}),
    "DaemonSet": frozenset({"apps/v1"}),
    "Deployment": frozenset({"apps/v1"}),
    "Job": frozenset({"batch/v1"}),
    "Pod": frozenset({"v1"}),
    "ReplicaSet": frozenset({"apps/v1"}),
    "ReplicationController": frozenset({"v1"}),
    "StatefulSet": frozenset({"apps/v1"}),
}

#: Where each workload kind keeps its pod spec.
POD_SPEC_PATHS: Mapping[str, tuple[str, ...]] = {
    "CronJob": ("spec", "jobTemplate", "spec", "template", "spec"),
    "DaemonSet": ("spec", "template", "spec"),
    "Deployment": ("spec", "template", "spec"),
    "Job": ("spec", "template", "spec"),
    "Pod": ("spec",),
    "ReplicaSet": ("spec", "template", "spec"),
    "ReplicationController": ("spec", "template", "spec"),
    "StatefulSet": ("spec", "template", "spec"),
}

#: The API version GreenKube writes in an action target for each recommendable workload kind.
ACTION_API_VERSIONS: Mapping[str, str] = {
    "CronJob": "batch/v1",
    "DaemonSet": "apps/v1",
    "Deployment": "apps/v1",
    "Job": "batch/v1",
    "ReplicaSet": "apps/v1",
    "ReplicationController": "v1",
    "StatefulSet": "apps/v1",
}


def pod_template_metadata_path(kind: str) -> tuple[str, ...] | None:
    """Path of the metadata whose ``checksum/*`` annotations a renderer may recompute."""
    pod_spec_path = POD_SPEC_PATHS.get(kind)
    if pod_spec_path is None:
        return None
    return ("metadata",) if kind == "Pod" else (*pod_spec_path[:-1], "metadata")


def container_names(manifest: Mapping[str, Any], kind: str) -> tuple[str, ...] | None:
    """Ordered container names of a rendered workload, or ``None`` when they are not unique strings."""
    current: Any = manifest
    for field_name in POD_SPEC_PATHS.get(kind, ()):
        if not isinstance(current, Mapping):
            return None
        current = current.get(field_name)
    if not isinstance(current, Mapping):
        return None
    containers = current.get("containers")
    if not isinstance(containers, list):
        return None
    names: list[str] = []
    for container in containers:
        if not isinstance(container, Mapping):
            return None
        name = container.get("name")
        if not isinstance(name, str) or not name:
            return None
        names.append(name)
    return tuple(names) if len(names) == len(set(names)) else None
