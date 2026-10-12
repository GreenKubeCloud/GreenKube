# src/greenkube/core/optimization/applied_detector.py
"""Detects applied recommendations from the live cluster state (Phase 3).

Apply detection uses the Kubernetes API: when a targeted container's live
request matches the recommendation within the configured tolerance and an
active recommendation existed for that target, the recommendation transitions
to ``applied``. Ambiguous workload-level aggregates are never used to infer a
multi-container target.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional, Protocol, Sequence, Tuple

from greenkube.gitops.contracts import (
    ActionPlanV3,
    ActionStatus,
    PlanStatus,
    PreApplyWorkloadBaseline,
    SetContainerResources,
)
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
    container_requests: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    uid: str | None = None
    generation: int | None = None
    observed_generation: int | None = None
    container_request_values: Dict[str, Tuple[int | None, int | None]] = field(default_factory=dict)
    container_request_values_valid: bool = True


class WorkloadReader(Protocol):
    """Minimal reader contract so tests can inject fakes."""

    async def read(self) -> Dict[Tuple[str, str, str], WorkloadSnapshot]: ...


class ActionStatusRepository(Protocol):
    """Read and update GitOps action lifecycle state for one recommendation."""

    async def get_recommendation_action_status(self, recommendation_id: int) -> ActionStatus | None: ...

    async def set_recommendation_action_status(
        self,
        recommendation_ids,
        status: ActionStatus,
        *,
        expected_statuses: Sequence[ActionStatus] | None = None,
        actor: str = "system",
        plan_id: str | None = None,
    ) -> int: ...

    async def list_plans_for_recommendation(self, recommendation_id: int) -> Sequence["SyncPlanStatus"]: ...

    async def set_plan_status(
        self,
        plan_id: str,
        status: PlanStatus,
        *,
        expected_statuses: Sequence[PlanStatus] | None = None,
        expected_digest: str | None = None,
        actor: str = "system",
        reason_code: str | None = None,
    ) -> "SyncPlanStatus | None": ...


class SyncPlanStatus(Protocol):
    @property
    def plan_id(self) -> str: ...

    @property
    def digest(self) -> str | None: ...

    @property
    def recommendation_ids(self) -> list[int]: ...

    @property
    def created_at(self) -> datetime: ...

    @property
    def status(self) -> PlanStatus: ...

    @property
    def rollback_of_plan_id(self) -> str | None: ...

    @property
    def action(self) -> ActionPlanV3: ...

    @property
    def pre_apply_baseline(self) -> PreApplyWorkloadBaseline | None: ...


def _sum_requests(containers) -> tuple[int, int, Dict[str, Tuple[int, int]]]:
    cpu = 0
    memory = 0
    per_container: Dict[str, Tuple[int, int]] = {}
    for container in containers or []:
        container_name = _resource_field(container, "name")
        if not isinstance(container_name, str) or not container_name:
            continue
        resources = _resource_field(container, "resources")
        requests = _resource_field(resources, "requests") if resources else None
        container_cpu = _parse_cpu(requests.get("cpu")) if isinstance(requests, Mapping) else 0
        container_memory = _parse_memory(requests.get("memory")) if isinstance(requests, Mapping) else 0
        per_container[container_name] = (container_cpu, container_memory)
        cpu += container_cpu
        memory += container_memory
    return cpu, memory, per_container


def _resource_field(value, name: str, camel_case: str | None = None):
    if isinstance(value, Mapping):
        if name in value:
            return value[name]
        return value.get(camel_case) if camel_case is not None else None
    result = getattr(value, name, None)
    return result if result is not None or camel_case is None else getattr(value, camel_case, None)


def _read_container_request_values(
    containers,
) -> tuple[Dict[str, Tuple[int | None, int | None]], bool]:
    values: Dict[str, Tuple[int | None, int | None]] = {}
    valid = True
    from greenkube.utils.k8s_quantities import parse_cpu_quantity, parse_memory_quantity

    for container in containers or []:
        name = _resource_field(container, "name")
        if not isinstance(name, str) or not name or name != name.strip() or name in values:
            valid = False
            continue
        resources = _resource_field(container, "resources")
        requests = _resource_field(resources, "requests") if resources else None
        if requests is None:
            requests = {}
        elif not isinstance(requests, Mapping):
            valid = False
            requests = {}

        cpu, cpu_valid = _parse_baseline_quantity(requests.get("cpu"), parse_cpu_quantity)
        memory, memory_valid = _parse_baseline_quantity(requests.get("memory"), parse_memory_quantity)
        valid = valid and cpu_valid and memory_valid
        values[name] = (cpu, memory)
    return values, valid


def _parse_baseline_quantity(value, parser) -> tuple[int | None, bool]:
    if value is None:
        return None, True
    try:
        parsed = parser(value)
    except Exception:
        return None, False
    if isinstance(parsed, bool) or not isinstance(parsed, int) or parsed < 0:
        return None, False
    return parsed, True


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
                metadata = _resource_field(item, "metadata")
                spec = _resource_field(item, "spec")
                template = _resource_field(spec, "template") if spec else None
                pod_spec = _resource_field(template, "spec") if template else None
                if not metadata or not pod_spec:
                    continue
                containers = _resource_field(pod_spec, "containers")
                cpu, memory, container_requests = _sum_requests(containers)
                request_values, request_values_valid = _read_container_request_values(containers)
                status = _resource_field(item, "status")
                namespace = _resource_field(metadata, "namespace")
                name = _resource_field(metadata, "name")
                if not isinstance(namespace, str) or not namespace or not isinstance(name, str) or not name:
                    continue
                snapshots[(namespace, kind, name)] = WorkloadSnapshot(
                    namespace=namespace,
                    kind=kind,
                    name=name,
                    cpu_request_millicores=cpu,
                    memory_request_bytes=memory,
                    container_requests=container_requests,
                    uid=_resource_field(metadata, "uid"),
                    generation=_resource_field(metadata, "generation"),
                    observed_generation=_resource_field(status, "observed_generation", "observedGeneration"),
                    container_request_values=request_values,
                    container_request_values_valid=request_values_valid,
                )
        return snapshots


class AppliedDetector:
    """Transitions recommendations to ``applied`` when the change has landed."""

    def __init__(
        self,
        lifecycle: "RecommendationLifecycle",
        reader: Optional[WorkloadReader] = None,
        config: Optional["Config"] = None,
        action_status_repository: Optional[ActionStatusRepository] = None,
    ):
        from greenkube.core.config import get_config

        self.lifecycle = lifecycle
        self.reader = reader if reader is not None else K8sWorkloadReader()
        self.config = config if config is not None else get_config()
        self.action_status_repository = action_status_repository

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

        A decrease alone is not evidence of this recommendation being applied;
        the live request must match the proposed value within tolerance.
        """
        # A missing request (0) is not an applied change: it means the workload
        # declares no requests, not that the recommendation was implemented.
        if observed is None or observed <= 0 or current is None or current <= 0:
            return False
        if recommended is not None and recommended > 0:
            return abs(observed - recommended) <= recommended * tolerance
        return False

    async def detect(self) -> List[RecommendationRecord]:
        """Detects and records applied rightsizing recommendations."""
        records = await self.lifecycle.repo.get_recommendations_by_statuses(
            ["active", "pr_open", RecommendationStatus.ROLLBACK_REVIEW.value]
        )
        candidates = [r for r in records if r.type in RIGHTSIZING_TYPES]
        if not candidates:
            return []

        snapshots = await self.reader.read()
        if not snapshots:
            return []

        tolerance = float(getattr(self.config, "RECOMMENDATION_APPLY_TOLERANCE", 0.25))
        applied: List[RecommendationRecord] = []
        processed_rollbacks: set[str] = set()

        for record in candidates:
            if record.status is RecommendationStatus.ROLLBACK_REVIEW:
                if record.id is None:
                    continue
                rollback_plan = await self._latest_rollback_plan(record.id)
                if rollback_plan is None or rollback_plan.plan_id in processed_rollbacks:
                    continue
                processed_rollbacks.add(rollback_plan.plan_id)
                try:
                    applied.extend(await self._revert_synced_rollback(rollback_plan, snapshots))
                except Exception as exc:
                    logger.warning("Rollback verification failed for plan %s: %s", rollback_plan.plan_id, exc)
                continue

            action_status: ActionStatus | None = None
            synced_plan: SyncPlanStatus | None = None
            if record.action is not None:
                if self.action_status_repository is None or record.id is None:
                    continue
                raw_status = await self.action_status_repository.get_recommendation_action_status(record.id)
                try:
                    action_status = ActionStatus(raw_status) if raw_status is not None else None
                except (TypeError, ValueError):
                    logger.warning(
                        "Apply detection skipped recommendation %s with an invalid action status.", record.id
                    )
                    continue
                if action_status is not ActionStatus.SYNC_PENDING:
                    continue
                synced_plan = await self._synced_forward_plan(record.id)
                if synced_plan is None:
                    continue
            elif self.action_status_repository is not None and record.id is not None:
                try:
                    raw_status = await self.action_status_repository.get_recommendation_action_status(record.id)
                    current_action_status = ActionStatus(raw_status) if raw_status is not None else None
                except (TypeError, ValueError):
                    logger.warning(
                        "Apply detection skipped recommendation %s with an invalid action status.", record.id
                    )
                    continue
                except Exception as exc:
                    logger.warning(
                        "Apply detection skipped recommendation %s because action state could not be read: %s",
                        record.id,
                        exc,
                    )
                    continue
                if current_action_status not in {None, ActionStatus.NONE, ActionStatus.PR_OPEN}:
                    continue
                if current_action_status is ActionStatus.PR_OPEN:
                    try:
                        linked_plans = await self.action_status_repository.list_plans_for_recommendation(record.id)
                    except Exception as exc:
                        logger.warning(
                            "Apply detection skipped recommendation %s because its plans could not be read: %s",
                            record.id,
                            exc,
                        )
                        continue
                    if any(record.id in plan.recommendation_ids for plan in linked_plans):
                        continue

            key = self._target_key(record)
            if key is None:
                continue
            snapshot = snapshots.get(key)
            if snapshot is None:
                continue
            if record.action is not None and (
                synced_plan is None
                or not _workload_reconciled_after_baseline(snapshot, synced_plan.pre_apply_baseline)
                or not self._matches_plan_target(record, synced_plan, snapshot, tolerance)
            ):
                continue

            if record.type == RecommendationType.RIGHTSIZING_CPU:
                observed = _observed_request(snapshot, record.container_name, "cpu")
                current = record.current_cpu_request_millicores
                recommended = record.recommended_cpu_request_millicores
                actual_cpu: Optional[int] = observed
                actual_memory: Optional[int] = None
            else:
                observed = _observed_request(snapshot, record.container_name, "memory")
                current = record.current_memory_request_bytes
                recommended = record.recommended_memory_request_bytes
                actual_cpu = None
                actual_memory = observed

            if record.id is None or not self._decreased(observed, current, recommended, tolerance):
                continue

            method = (
                ApplicationMethod.PR_MERGE.value
                if action_status is not None
                else (
                    ApplicationMethod.PR_MERGE.value
                    if record.status == RecommendationStatus.PR_OPEN
                    else ApplicationMethod.DETECTED.value
                )
            )
            try:
                updated = await self.lifecycle.apply(
                    record.id,
                    actual_cpu=actual_cpu,
                    actual_memory=actual_memory,
                    application_method=method,
                    actor="detector",
                )
                if action_status is not None and self.action_status_repository is not None:
                    updated_count = await self.action_status_repository.set_recommendation_action_status(
                        [record.id],
                        ActionStatus.APPLIED,
                        expected_statuses=(action_status,),
                        actor="apply-detector",
                        plan_id=synced_plan.plan_id if synced_plan is not None else None,
                    )
                    if updated_count != 1:
                        raise RuntimeError("GitOps action state did not transition to applied.")
                    await self._mark_completed_plans(record.id)
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

    @staticmethod
    def _matches_plan_target(
        record: RecommendationRecord,
        plan: SyncPlanStatus,
        snapshot: WorkloadSnapshot,
        tolerance: float,
    ) -> bool:
        if (
            record.action != plan.action
            or record.container_name is None
            or plan.pre_apply_baseline is None
            or record.container_name not in plan.pre_apply_baseline.container_requests
            or (
                plan.action.target.namespace,
                plan.action.target.kind,
                plan.action.target.name,
            )
            != (snapshot.namespace, snapshot.kind, snapshot.name)
        ):
            return False

        operations = [
            operation
            for operation in plan.action.operations
            if isinstance(operation, SetContainerResources) and operation.container == record.container_name
        ]
        if len(operations) != 1:
            return False
        resource = "cpu" if record.type is RecommendationType.RIGHTSIZING_CPU else "memory"
        target_value = operations[0].resources.requests.get(resource)
        if target_value is None:
            return False
        if resource == "cpu":
            expected = _parse_cpu(target_value)
        else:
            expected = _parse_memory(target_value)
        observed = _observed_request(snapshot, record.container_name, resource)
        return observed is not None and expected > 0 and abs(observed - expected) <= expected * tolerance

    async def _latest_rollback_plan(self, recommendation_id: int) -> SyncPlanStatus | None:
        if self.action_status_repository is None:
            return None
        plans = await self.action_status_repository.list_plans_for_recommendation(recommendation_id)
        rollback_plans = [plan for plan in plans if getattr(plan, "rollback_of_plan_id", None) is not None]
        if not rollback_plans:
            return None
        latest = max(rollback_plans, key=lambda plan: (plan.created_at, plan.plan_id))
        if latest.status is not PlanStatus.SYNCED:
            return None
        return latest

    async def _revert_synced_rollback(
        self,
        rollback_plan: SyncPlanStatus,
        snapshots: Dict[Tuple[str, str, str], WorkloadSnapshot],
    ) -> list[RecommendationRecord]:
        if self.action_status_repository is None or rollback_plan.rollback_of_plan_id is None:
            return []
        recommendation_ids = list(rollback_plan.recommendation_ids)
        if not recommendation_ids or len(set(recommendation_ids)) != len(recommendation_ids):
            return []

        related_plans = await self.action_status_repository.list_plans_for_recommendation(recommendation_ids[0])
        original_plan = next(
            (plan for plan in related_plans if plan.plan_id == rollback_plan.rollback_of_plan_id),
            None,
        )
        if (
            original_plan is None
            or original_plan.status not in {PlanStatus.SYNCED, PlanStatus.APPLIED}
            or set(original_plan.recommendation_ids) != set(recommendation_ids)
            or original_plan.action != rollback_plan.action
            or original_plan.pre_apply_baseline is None
            or rollback_plan.pre_apply_baseline is None
        ):
            return []
        original_baseline = original_plan.pre_apply_baseline
        rollback_baseline = rollback_plan.pre_apply_baseline
        if not original_baseline.container_requests or not rollback_baseline.container_requests:
            return []

        operations = rollback_plan.action.operations
        if not operations or any(not isinstance(operation, SetContainerResources) for operation in operations):
            return []
        operation_containers = {
            operation.container for operation in operations if isinstance(operation, SetContainerResources)
        }

        records: list[RecommendationRecord] = []
        observations: dict[int, tuple[int | None, int | None]] = {}
        for recommendation_id in recommendation_ids:
            record = await self.lifecycle.repo.get_recommendation_by_id(recommendation_id)
            if (
                record is None
                or record.id != recommendation_id
                or record.status is not RecommendationStatus.ROLLBACK_REVIEW
                or record.scope != "workload"
                or record.action != rollback_plan.action
                or record.container_name is None
                or record.namespace is None
                or record.owner_kind is None
                or record.owner_name is None
                or record.container_name not in operation_containers
                or (
                    rollback_plan.action.target.namespace,
                    rollback_plan.action.target.kind,
                    rollback_plan.action.target.name,
                )
                != (record.namespace, record.owner_kind, record.owner_name)
            ):
                return []
            if await self.action_status_repository.get_recommendation_action_status(recommendation_id) is not (
                ActionStatus.SYNC_PENDING
            ):
                return []
            workload = snapshots.get((record.namespace, record.owner_kind, record.owner_name))
            if (
                workload is None
                or not _workload_reconciled_after_baseline(workload, rollback_baseline)
                or record.container_name not in original_baseline.container_requests
                or record.container_name not in rollback_baseline.container_requests
            ):
                return []
            live = _container_request_values(workload, record.container_name)
            expected = original_baseline.container_requests[record.container_name]
            if live is None or not _matches_pre_apply_requests(workload, live, expected):
                return []
            if record.type is RecommendationType.RIGHTSIZING_CPU:
                observations[recommendation_id] = (live[0], None)
            elif record.type is RecommendationType.RIGHTSIZING_MEMORY:
                observations[recommendation_id] = (None, live[1])
            else:
                return []
            records.append(record)

        if {record.container_name for record in records} != operation_containers:
            return []

        for recommendation_id in recommendation_ids:
            current = await self.lifecycle.repo.get_recommendation_by_id(recommendation_id)
            if current is None or current.status is not RecommendationStatus.ROLLBACK_REVIEW:
                return []
            if (
                await self.action_status_repository.get_recommendation_action_status(recommendation_id)
                is not ActionStatus.SYNC_PENDING
            ):
                return []

        reverted: list[RecommendationRecord] = []
        for record in records:
            assert record.id is not None
            actual_cpu, actual_memory = observations[record.id]
            reverted.append(
                await self.lifecycle.mark_reverted(
                    record.id,
                    rollback_plan_id=rollback_plan.plan_id,
                    rollback_of_plan_id=rollback_plan.rollback_of_plan_id,
                    actual_cpu_request_millicores=actual_cpu,
                    actual_memory_request_bytes=actual_memory,
                    actor="detector",
                )
            )
        updated_count = await self.action_status_repository.set_recommendation_action_status(
            recommendation_ids,
            ActionStatus.REVERTED,
            expected_statuses=(ActionStatus.SYNC_PENDING,),
            actor="apply-detector",
            plan_id=rollback_plan.plan_id,
        )
        if updated_count != len(recommendation_ids):
            raise RuntimeError("not every rollback recommendation action transitioned to reverted")
        updated_plan = await self.action_status_repository.set_plan_status(
            rollback_plan.plan_id,
            PlanStatus.APPLIED,
            expected_statuses=(rollback_plan.status,),
            expected_digest=rollback_plan.digest,
            actor="apply-detector",
        )
        if updated_plan is None or updated_plan.status is not PlanStatus.APPLIED:
            raise RuntimeError("fully reverted rollback plan did not transition to applied")
        return reverted

    async def _synced_forward_plan(self, recommendation_id: int) -> SyncPlanStatus | None:
        if self.action_status_repository is None:
            return None
        plans = await self.action_status_repository.list_plans_for_recommendation(recommendation_id)
        related = [plan for plan in plans if recommendation_id in plan.recommendation_ids]
        if not related:
            return None
        latest = max(related, key=lambda plan: (plan.created_at, plan.plan_id))
        if latest.rollback_of_plan_id is not None:
            return None
        return latest if latest.status in {PlanStatus.SYNCED, PlanStatus.APPLIED} else None

    async def _has_synced_plan(self, recommendation_id: int) -> bool:
        return await self._synced_forward_plan(recommendation_id) is not None

    async def _mark_completed_plans(self, recommendation_id: int) -> None:
        if self.action_status_repository is None:
            return
        plans = await self.action_status_repository.list_plans_for_recommendation(recommendation_id)
        for plan in plans:
            if plan.status is not PlanStatus.SYNCED:
                continue
            statuses = [
                await self.action_status_repository.get_recommendation_action_status(item_id)
                for item_id in plan.recommendation_ids
            ]
            if all(status is ActionStatus.APPLIED for status in statuses):
                updated_plan = await self.action_status_repository.set_plan_status(
                    plan.plan_id,
                    PlanStatus.APPLIED,
                    expected_statuses=(PlanStatus.SYNCED,),
                    expected_digest=plan.digest,
                    actor="apply-detector",
                )
                if updated_plan is None or updated_plan.status is not PlanStatus.APPLIED:
                    raise RuntimeError("fully applied GitOps plan did not transition to applied")


def _observed_request(snapshot: WorkloadSnapshot, container_name: str | None, resource: str) -> int | None:
    """Use a container's live request when available; never infer across sidecars."""
    if container_name:
        requests = snapshot.container_requests.get(container_name)
        if requests is None:
            return None
    elif snapshot.container_requests:
        if len(snapshot.container_requests) != 1:
            return None
        requests = next(iter(snapshot.container_requests.values()))
    else:
        # Compatibility for injected/legacy workload readers that only return
        # aggregates; the production Kubernetes reader always populates containers.
        return snapshot.cpu_request_millicores if resource == "cpu" else snapshot.memory_request_bytes
    return requests[0] if resource == "cpu" else requests[1]


def _workload_reconciled_after_baseline(
    snapshot: WorkloadSnapshot,
    baseline: PreApplyWorkloadBaseline | None,
) -> bool:
    workload_uid = snapshot.uid
    generation = snapshot.generation
    observed_generation = snapshot.observed_generation
    valid_uid = isinstance(workload_uid, str) and bool(workload_uid.strip()) and workload_uid == workload_uid.strip()
    workload_changed = (
        (
            workload_uid != baseline.workload_uid
            or (isinstance(generation, int) and not isinstance(generation, bool) and generation > baseline.generation)
        )
        if baseline is not None
        else False
    )
    return (
        baseline is not None
        and getattr(snapshot, "container_request_values_valid", True)
        and valid_uid
        and workload_changed
        and isinstance(generation, int)
        and not isinstance(generation, bool)
        and generation > 0
        and isinstance(observed_generation, int)
        and not isinstance(observed_generation, bool)
        and observed_generation >= generation
        and baseline.observed_generation >= baseline.generation
    )


def _container_request_values(
    snapshot: WorkloadSnapshot,
    container_name: str,
) -> tuple[int | None, int | None] | None:
    values = snapshot.container_request_values if snapshot.container_request_values else snapshot.container_requests
    requests = values.get(container_name)
    if requests is None or not isinstance(requests, (tuple, list)) or len(requests) != 2:
        return None
    cpu, memory = requests
    for value in (cpu, memory):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            return None
    return cpu, memory


def _matches_pre_apply_requests(
    snapshot: WorkloadSnapshot,
    actual: tuple[int | None, int | None],
    expected,
) -> bool:
    presence_is_known = bool(snapshot.container_request_values)
    for current_value, baseline_value in (
        (actual[0], expected.cpu_millicores),
        (actual[1], expected.memory_bytes),
    ):
        if baseline_value is None:
            if presence_is_known and current_value is not None:
                return False
            if not presence_is_known and current_value not in {None, 0}:
                return False
        elif current_value != baseline_value:
            return False
    return True
