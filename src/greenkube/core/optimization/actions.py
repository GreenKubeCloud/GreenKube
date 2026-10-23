"""Build typed, explicitly container-targeted GitOps actions."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Sequence

from greenkube.gitops.contracts import ActionPlanV3, ContainerResources, ResourceTarget, SetContainerResources
from greenkube.models.metrics import Recommendation, RecommendationType

_API_VERSIONS_BY_KIND = {
    "CronJob": "batch/v1",
    "DaemonSet": "apps/v1",
    "Deployment": "apps/v1",
    "Job": "batch/v1",
    "ReplicaSet": "apps/v1",
    "ReplicationController": "v1",
    "StatefulSet": "apps/v1",
}

_MEMORY_UNITS = (
    ("Ei", 1024**6),
    ("Pi", 1024**5),
    ("Ti", 1024**4),
    ("Gi", 1024**3),
    ("Mi", 1024**2),
    ("Ki", 1024),
)


@dataclass
class _ContainerActionGroup:
    requests: dict[str, str] = field(default_factory=dict)
    recommendation_ids: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    conflicts: set[str] = field(default_factory=set)


@dataclass
class _ActionGroup:
    target: ResourceTarget
    containers: dict[str, _ContainerActionGroup] = field(default_factory=dict)


def _cpu_quantity(millicores: int) -> str:
    """Format a positive millicore value as a canonical CPU quantity."""
    cores, remainder = divmod(millicores, 1000)
    return str(cores) if remainder == 0 else f"{millicores}m"


def _memory_quantity(byte_count: int) -> str:
    """Format a positive byte value using its largest exact binary unit."""
    for suffix, factor in _MEMORY_UNITS:
        if byte_count % factor == 0:
            return f"{byte_count // factor}{suffix}"
    return str(byte_count)


def _resource_proposal(recommendation: Recommendation) -> tuple[str, str] | None:
    """Return a resource quantity only when its per-container evidence agrees."""
    evidence = recommendation.evidence
    patch = evidence.proposed_patch if evidence else None
    container_name = recommendation.container_name
    if (
        evidence is None
        or patch is None
        or not container_name
        or container_name.strip() != container_name
        or patch.container_name != container_name
    ):
        return None

    if recommendation.type == RecommendationType.RIGHTSIZING_CPU:
        resource = "cpu"
        current = evidence.current.cpu_request_millicores
        proposed = recommendation.recommended_cpu_request_millicores
        evidence_proposed = evidence.proposed.cpu_request_millicores
        recommendation_current = recommendation.current_cpu_request_millicores
        quantity = _cpu_quantity(proposed) if proposed and proposed > 0 else None
    elif recommendation.type == RecommendationType.RIGHTSIZING_MEMORY:
        resource = "memory"
        current = evidence.current.memory_request_bytes
        proposed = recommendation.recommended_memory_request_bytes
        evidence_proposed = evidence.proposed.memory_request_bytes
        recommendation_current = recommendation.current_memory_request_bytes
        quantity = _memory_quantity(proposed) if proposed and proposed > 0 else None
    else:
        return None

    if (
        current is None
        or proposed is None
        or recommendation_current != current
        or evidence_proposed != proposed
        or proposed >= current
        or quantity is None
    ):
        return None
    if not any(
        change.resource == resource and change.current == current and change.proposed == proposed
        for change in evidence.changes
    ):
        return None
    return resource, quantity


def _target_for(recommendation: Recommendation) -> tuple[ResourceTarget, str] | None:
    """Resolve only known, manifest-addressable workload kinds."""
    kind = recommendation.owner_kind
    name = recommendation.owner_name
    namespace = recommendation.namespace
    container_name = recommendation.container_name
    if (
        not kind
        or kind.strip() != kind
        or not name
        or name.strip() != name
        or not namespace
        or namespace.strip() != namespace
        or not container_name
        or container_name.strip() != container_name
    ):
        return None
    api_version = _API_VERSIONS_BY_KIND.get(kind)
    if api_version is None:
        return None
    return (
        ResourceTarget(api_version=api_version, kind=kind, namespace=namespace, name=name),
        container_name,
    )


def _action_id(
    target: ResourceTarget,
    operations: list[SetContainerResources],
    recommendation_ids: list[int],
) -> str:
    payload = {
        "target": target.model_dump(mode="json"),
        "recommendation_ids": recommendation_ids,
        "operations": [operation.model_dump(mode="json") for operation in operations],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"action-{digest[:20]}"


def build_action_plans(recommendations: Sequence[Recommendation]) -> dict[int, ActionPlanV3]:
    """Build one workload change set with an explicit operation per container."""
    groups: dict[tuple[str, str, str], _ActionGroup] = {}
    seen_ids: set[int] = set()

    for recommendation in recommendations:
        recommendation_id = recommendation.id
        if recommendation_id is None or recommendation_id <= 0 or recommendation_id in seen_ids:
            continue

        target = _target_for(recommendation)
        proposal = _resource_proposal(recommendation)
        if target is None or proposal is None:
            continue

        resource, quantity = proposal
        resource_target, container_name = target
        key = (resource_target.namespace, resource_target.kind, resource_target.name)
        group = groups.setdefault(key, _ActionGroup(resource_target))
        container_group = group.containers.setdefault(container_name, _ContainerActionGroup())
        existing = container_group.requests.get(resource)
        if existing is not None and existing != quantity:
            container_group.conflicts.add(resource)
        else:
            container_group.requests[resource] = quantity
            container_group.recommendation_ids[resource].add(recommendation_id)
            seen_ids.add(recommendation_id)

    actions: dict[int, ActionPlanV3] = {}
    for group in groups.values():
        operations: list[SetContainerResources] = []
        recommendation_ids: set[int] = set()
        for container_name, container_group in sorted(group.containers.items()):
            requests = {
                resource: value
                for resource, value in container_group.requests.items()
                if resource not in container_group.conflicts
            }
            container_recommendations = {
                recommendation_id
                for resource, ids in container_group.recommendation_ids.items()
                if resource not in container_group.conflicts
                for recommendation_id in ids
            }
            if not requests or not container_recommendations:
                continue
            recommendation_ids.update(container_recommendations)
            operations.append(
                SetContainerResources(
                    container=container_name,
                    resources=ContainerResources(requests=requests),
                )
            )
        ordered_recommendations = sorted(recommendation_ids)
        if not ordered_recommendations or not operations:
            continue
        plan = ActionPlanV3(
            action_id=_action_id(group.target, operations, ordered_recommendations),
            recommendation_ids=ordered_recommendations,
            target=group.target,
            operations=operations,
            constraints={"container_targeted": True},
            evidence_ref="recommendation-evidence/" + "/".join(str(item) for item in ordered_recommendations),
        )
        for recommendation_id in ordered_recommendations:
            actions[recommendation_id] = plan

    return actions
