"""The ``container_resources`` action kind: per-container CPU/memory requests and limits (H0a).

Everything specific to ``SetContainerResources`` lives here: building actions from
rightsizing recommendations, compiling to primitive field edits, request <= limit
invariants, baseline coverage, the no-effective-change check, apply detection, the
baseline-derived inverse and the pull-request section.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import ValidationError

from greenkube.automation.actions.base import (
    ActionBaseline,
    ActionInvariantError,
    ActionKindError,
    ActionKindSpec,
    AppliedVerdict,
    InverseConflictError,
    InverseUnsupportedError,
    PrSection,
    RiskClass,
    Support,
)
from greenkube.automation.actions.primitives import ListSelector, PathSegment, PrimitiveOp, SetField, format_path
from greenkube.automation.actions.workloads import ACTION_API_VERSIONS, POD_SPEC_PATHS, WORKLOAD_API_VERSIONS
from greenkube.gitops.contracts import (
    ActionPlanV3,
    ContainerResources,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    TypedOperation,
)

if TYPE_CHECKING:
    from greenkube.models.metrics import Recommendation

_MEMORY_UNITS = (
    ("Ei", 1024**6),
    ("Pi", 1024**5),
    ("Ti", 1024**4),
    ("Gi", 1024**3),
    ("Mi", 1024**2),
    ("Ki", 1024),
)
_QUANTITY_PATTERN = re.compile(
    r"(?P<number>(?:\d+(?:\.\d*)?|\.\d+))"
    r"(?P<suffix>[eE][+-]?\d+|Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|K|M|G|T|P|E)?"
)
_DECIMAL_SCALES = {
    "n": Fraction(1, 1_000_000_000),
    "u": Fraction(1, 1_000_000),
    "m": Fraction(1, 1_000),
    "k": Fraction(1_000),
    "K": Fraction(1_000),
    "M": Fraction(1_000_000),
    "G": Fraction(1_000_000_000),
    "T": Fraction(1_000_000_000_000),
    "P": Fraction(1_000_000_000_000_000),
    "E": Fraction(1_000_000_000_000_000_000),
}
_BINARY_SCALES = {"Ki": 1, "Mi": 2, "Gi": 3, "Ti": 4, "Pi": 5, "Ei": 6}
_MAX_KUBERNETES_QUANTITY = Fraction(2**63 - 1)
#: Requests a GreenKube-managed Kustomize or post-renderer patch may carry.
_PATCHABLE_REQUESTS = frozenset({"cpu", "memory"})


class ContainerResourcesSpec(ActionKindSpec):
    """``set_container_resources``: set CPU/memory requests (and limits) of named containers."""

    kind: ClassVar[str] = "container_resources"
    operation_model: ClassVar[type[TypedOperation]] = SetContainerResources
    capability: ClassVar[str] = "rightsizing"
    risk_class: ClassVar[RiskClass] = "low"
    measurement_strategy: ClassVar[str] = "container_resources"
    pr_label: ClassVar[str] = "rightsizing"
    title: ClassVar[str] = "per-container rightsizing"
    description: ClassVar[str] = "container resources"

    # -- recommendations ---------------------------------------------------------------
    def ops_for(self, recommendations: Sequence[Recommendation]) -> dict[int, ActionPlanV3]:
        return build_container_resource_actions(recommendations)

    # -- validation and compilation ------------------------------------------------------
    def validate_operations(self, target: ResourceTarget, operations: Sequence[TypedOperation]) -> None:
        supported_versions = WORKLOAD_API_VERSIONS.get(target.kind)
        if supported_versions is None or target.api_version not in supported_versions:
            raise ActionKindError(f"unsupported workload kind or API version: {target.api_version}/{target.kind}")
        resource_operations = container_operations(operations)
        names = [operation.container for operation in resource_operations]
        if len(names) != len(set(names)):
            raise ActionKindError("each container may have only one per-container rightsizing operation")
        if any(not op.resources.requests and not op.resources.limits for op in resource_operations):
            raise ActionKindError("a rightsizing operation must set at least one resource quantity")

    def subjects(self, operations: Sequence[TypedOperation]) -> set[str]:
        return {operation.container for operation in container_operations(operations)}

    def changed_fields(self, operations: Sequence[TypedOperation]) -> set[tuple[str, str]]:
        return {
            (operation.container, resource.lower())
            for operation in container_operations(operations)
            for resource in {*operation.resources.requests, *operation.resources.limits}
        }

    def compile(self, target: ResourceTarget, operations: Sequence[TypedOperation]) -> list[PrimitiveOp]:
        pod_spec_path = POD_SPEC_PATHS.get(target.kind)
        if pod_spec_path is None:
            raise ActionKindError(f"unsupported workload kind: {target.kind}")
        primitives: list[PrimitiveOp] = []
        for operation in container_operations(operations):
            for section, values in (("requests", operation.resources.requests), ("limits", operation.resources.limits)):
                for resource_name, quantity in values.items():
                    path = format_path(
                        (
                            *pod_spec_path,
                            "containers",
                            ListSelector("name", operation.container),
                            "resources",
                            section,
                            resource_name,
                        )
                    )
                    primitives.append(SetField(ref=target, path=path, value=quantity))
        return primitives

    def validate_target(self, manifest: Mapping[str, Any], target: ResourceTarget) -> None:
        pod_spec(manifest, target.kind)
        if not isinstance(pod_spec(manifest, target.kind).get("containers"), list):
            raise ActionKindError(f"{target.kind} target must contain a containers list")

    def check_invariants(
        self,
        manifest: MutableMapping[str, Any],
        target: ResourceTarget,
        operations: Sequence[TypedOperation],
    ) -> None:
        """Every changed container keeps valid quantities and requests within limits."""
        containers = workload_containers(pod_spec(manifest, target.kind))
        for operation in container_operations(operations):
            container = find_container(containers, operation.container)
            raw_resources = container.get("resources", {})
            if not isinstance(raw_resources, Mapping):
                raise ActionInvariantError(f"container {operation.container!r} resources must be a mapping")
            try:
                resources = ContainerResources(
                    requests=dict(raw_resources.get("requests", {})),
                    limits=dict(raw_resources.get("limits", {})),
                )
            except (ValidationError, TypeError, ValueError) as exc:
                raise ActionInvariantError(
                    f"container {operation.container!r} has an invalid Kubernetes resource quantity or mapping"
                ) from exc
            request_values = {name: quantity_value(quantity) for name, quantity in resources.requests.items()}
            limit_values = {name: quantity_value(quantity) for name, quantity in resources.limits.items()}
            for resource_name in resources.requests:
                limit = limit_values.get(resource_name)
                if limit is not None and request_values[resource_name] > limit:
                    raise ActionInvariantError(
                        f"container {operation.container!r} request for {resource_name!r} exceeds its limit"
                    )

    def owns_patch_field(self, kind: str, segments: Sequence[PathSegment], value: Any) -> bool:
        pod_spec_path = POD_SPEC_PATHS.get(kind)
        if pod_spec_path is None or len(segments) != len(pod_spec_path) + 5:
            return False
        prefix, rest = tuple(segments[: len(pod_spec_path)]), tuple(segments[len(pod_spec_path) :])
        return (
            prefix == pod_spec_path
            and rest[0] == "containers"
            and isinstance(rest[1], ListSelector)
            and rest[1].key == "name"
            and rest[2:4] == ("resources", "requests")
            and rest[4] in _PATCHABLE_REQUESTS
            and isinstance(value, str)
            and bool(value)
            and value == value.strip()
        )

    def renderer_support(self, renderer: RendererKind, operations: Sequence[TypedOperation]) -> Support:
        if renderer is RendererKind.KUSTOMIZE:
            for operation in container_operations(operations):
                if not operation.resources.requests:
                    return Support.unsupported("each operation must set at least one container request")
                if operation.resources.limits:
                    return Support.unsupported("Kustomize v1 supports requests only; limits edits are unsupported")
        return Support.supported()

    # -- baseline, apply detection, inverse ------------------------------------------------
    def effective_change(self, operations: Sequence[TypedOperation], baseline: ActionBaseline) -> bool | None:
        """``False`` when every proposed request equals the live request after unit normalization."""
        proposed: list[tuple[int | None, int | None]] = []
        for operation in container_operations(operations):
            live = baseline.container_requests.get(operation.container)
            if live is None:
                return None
            for resource, value in operation.resources.requests.items():
                parsed = parse_request_quantity(resource, value)
                if parsed is None:
                    return None
                proposed.append((parsed, live.cpu_millicores if resource == "cpu" else live.memory_bytes))
        if not proposed:
            return None
        return not all(value == current for value, current in proposed)

    def is_applied(
        self,
        operations: Sequence[TypedOperation],
        *,
        subject: str,
        field_name: str,
        observed: int | None,
        tolerance: float,
    ) -> AppliedVerdict:
        matches = [operation for operation in container_operations(operations) if operation.container == subject]
        if len(matches) != 1:
            return AppliedVerdict(applied=False, observed=observed, reason="no single operation for the container")
        target_value = matches[0].resources.requests.get(field_name)
        if target_value is None:
            return AppliedVerdict(applied=False, observed=observed, reason=f"no planned {field_name} request")
        expected = _lenient_quantity(field_name, target_value)
        applied = observed is not None and expected > 0 and abs(observed - expected) <= expected * tolerance
        return AppliedVerdict(applied=applied, expected=expected, observed=observed)

    def validate_inverse_scope(self, operations: Sequence[TypedOperation], baseline: ActionBaseline) -> None:
        """Rollback restores CPU/memory requests of containers the persisted baseline recorded."""
        try:
            resource_operations = container_operations(operations)
        except ActionKindError as exc:
            raise InverseUnsupportedError("rollback requires per-container resource operations") from exc
        if not resource_operations:
            raise InverseUnsupportedError("rollback requires per-container resource operations")
        seen: set[str] = set()
        for operation in resource_operations:
            requests = operation.resources.requests
            if not requests or operation.resources.limits:
                raise InverseUnsupportedError(
                    "rollback can restore only CPU and memory requests from the persisted baseline"
                )
            if set(requests) - {"cpu", "memory"}:
                raise InverseUnsupportedError(
                    "rollback cannot restore this resource request from the persisted baseline"
                )
            if operation.container in seen:
                raise InverseUnsupportedError("rollback requires one operation per container")
            seen.add(operation.container)
        if not seen.issubset(baseline.container_requests):
            raise InverseConflictError("the persisted pre-apply baseline omits an action container")

    def inverse(self, operations: Sequence[TypedOperation], baseline: ActionBaseline) -> list[TypedOperation]:
        """Restore the persisted pre-apply CPU/memory requests of every changed container."""
        self.validate_inverse_scope(operations, baseline)
        inverse: list[TypedOperation] = []
        for operation in container_operations(operations):
            container_baseline = baseline.container_requests[operation.container]
            restored: dict[str, str] = {}
            for resource in operation.resources.requests:
                value = container_baseline.cpu_millicores if resource == "cpu" else container_baseline.memory_bytes
                if value is None or isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise InverseConflictError(f"the persisted pre-apply baseline omits the target {resource} request")
                restored[resource] = f"{value}m" if resource == "cpu" else str(value)
            try:
                inverse.append(
                    SetContainerResources(
                        container=operation.container, resources=ContainerResources(requests=restored)
                    )
                )
            except ValidationError as exc:
                raise InverseConflictError("the persisted pre-apply baseline cannot form a valid inverse") from exc
        return inverse

    def supports_rollback(self, operations: Sequence[TypedOperation]) -> bool:
        return bool(operations) and all(isinstance(operation, SetContainerResources) for operation in operations)

    def pr_section(self, operations: Sequence[TypedOperation], recommendation: Any = None) -> PrSection:
        del recommendation
        lines: list[str] = []
        for operation in container_operations(operations):
            parts = [
                f"{section} {name}={value}"
                for section, values in (
                    ("requests", operation.resources.requests),
                    ("limits", operation.resources.limits),
                )
                for name, value in values.items()
            ]
            lines.append(f"{operation.container}: {', '.join(parts)}")
        return PrSection(label=self.pr_label, title="Container resources", lines=tuple(lines))


CONTAINER_RESOURCES = ContainerResourcesSpec()


def container_operations(operations: Sequence[TypedOperation]) -> list[SetContainerResources]:
    """The operations as ``SetContainerResources``; any other operation is rejected."""
    resource_operations: list[SetContainerResources] = []
    for operation in operations:
        if not isinstance(operation, SetContainerResources):
            raise ActionKindError("only per-container rightsizing operations are supported")
        resource_operations.append(operation)
    return resource_operations


def order_by_container(
    operations: Sequence[SetContainerResources], container_order: Sequence[str] | None
) -> list[SetContainerResources]:
    """Order operations like the rendered container list (strategic-merge patches keep list order)."""
    if container_order is None:
        return list(operations)
    positions = {name: index for index, name in enumerate(container_order)}
    return sorted(
        operations, key=lambda operation: (positions.get(operation.container, len(positions)), operation.container)
    )


# -- manifest helpers ------------------------------------------------------------------------
def pod_spec(manifest: Mapping[str, Any], workload_kind: str) -> MutableMapping[str, Any]:
    path = POD_SPEC_PATHS.get(workload_kind)
    if path is None:
        raise ActionKindError(f"unsupported workload kind: {workload_kind}")
    current: Any = manifest
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            raise ActionKindError(f"{workload_kind} target is missing its pod specification")
        current = current[part]
    if not isinstance(current, MutableMapping):
        raise ActionKindError(f"{workload_kind} pod specification must be a mapping")
    return current


def workload_containers(spec: MutableMapping[str, Any]) -> list[MutableMapping[str, Any]]:
    values = spec.get("containers")
    if not isinstance(values, list):
        raise ActionKindError("workload target must contain a containers list")
    if any(not isinstance(value, MutableMapping) for value in values):
        raise ActionKindError("workload containers must be mappings")
    return values


def find_container(containers: list[MutableMapping[str, Any]], name: str) -> MutableMapping[str, Any]:
    matches = [container for container in containers if container.get("name") == name]
    if len(matches) != 1:
        raise ActionKindError(f"container {name!r} must exist exactly once in the workload")
    return matches[0]


def quantity_value(quantity: str) -> Fraction:
    """Exact value of a Kubernetes quantity (fails closed on precision or range)."""
    if len(quantity) > 128:
        raise ActionInvariantError("Kubernetes resource quantity exceeds the supported precision")
    match = _QUANTITY_PATTERN.fullmatch(quantity)
    if match is None:
        raise ActionInvariantError(f"invalid Kubernetes resource quantity {quantity!r}")
    number = match.group("number")
    suffix = match.group("suffix") or ""
    try:
        if re.fullmatch(r"[eE][+-]?\d+", suffix):
            exponent = int(suffix[1:])
            if abs(exponent) > 1_000:
                raise ActionInvariantError("Kubernetes resource quantity exponent is out of range")
            value = Fraction(Decimal(f"{number}{suffix}"))
        else:
            value = Fraction(Decimal(number))
            if suffix in _DECIMAL_SCALES:
                value *= _DECIMAL_SCALES[suffix]
            elif suffix in _BINARY_SCALES:
                value *= 1_024 ** _BINARY_SCALES[suffix]
    except (InvalidOperation, OverflowError, ValueError) as exc:
        raise ActionInvariantError(f"invalid Kubernetes resource quantity {quantity!r}") from exc
    if value < 0 or value > _MAX_KUBERNETES_QUANTITY:
        raise ActionInvariantError(f"Kubernetes resource quantity {quantity!r} is outside the supported range")
    return value


def parse_request_quantity(resource: str, value: object) -> int | None:
    """Normalize a request to millicores or bytes; ``None`` when it cannot be compared."""
    from greenkube.utils.k8s_quantities import parse_cpu_quantity, parse_memory_quantity

    parser = {"cpu": parse_cpu_quantity, "memory": parse_memory_quantity}.get(resource)
    if parser is None or not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        parsed = parser(value)
    except Exception:
        return None
    return parsed if isinstance(parsed, int) and not isinstance(parsed, bool) else None


def _lenient_quantity(resource: str, value: object) -> int:
    """Apply detection's parser: millicores or bytes, 0 when unparseable."""
    from greenkube.utils.k8s_quantities import parse_cpu_quantity, parse_memory_quantity

    if value is None:
        return 0
    parser = parse_cpu_quantity if resource == "cpu" else parse_memory_quantity
    try:
        return parser(value) or 0
    except Exception:
        return 0


# -- recommendations -> actions ----------------------------------------------------------------
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
    from greenkube.models.metrics import RecommendationType

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
    api_version = ACTION_API_VERSIONS.get(kind)
    if api_version is None:
        return None
    return (
        ResourceTarget(api_version=api_version, kind=kind, namespace=namespace, name=name),
        container_name,
    )


def _action_id(target: ResourceTarget, operations: list[SetContainerResources], recommendation_ids: list[int]) -> str:
    payload = {
        "target": target.model_dump(mode="json"),
        "recommendation_ids": recommendation_ids,
        "operations": [operation.model_dump(mode="json") for operation in operations],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"action-{digest[:20]}"


def build_container_resource_actions(recommendations: Sequence[Recommendation]) -> dict[int, ActionPlanV3]:
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
                SetContainerResources(container=container_name, resources=ContainerResources(requests=requests))
            )
        ordered_recommendations = sorted(recommendation_ids)
        if not ordered_recommendations or not operations:
            continue
        plan = ActionPlanV3(
            action_id=_action_id(group.target, operations, ordered_recommendations),
            recommendation_ids=ordered_recommendations,
            target=group.target,
            operations=list(operations),
            constraints={"container_targeted": True},
            evidence_ref="recommendation-evidence/" + "/".join(str(item) for item in ordered_recommendations),
        )
        for recommendation_id in ordered_recommendations:
            actions[recommendation_id] = plan
    return actions
