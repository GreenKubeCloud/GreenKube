# src/greenkube/api/routers/manual_apply.py
"""Manual mode for clusters without GitOps (roadmap H9).

``GET /api/v1/recommendations/{id}/manual`` returns what to run by hand: a ``kubectl
patch`` for a typed change (built from the same primitives a pull request would write), a
``kubectl delete`` for an orphaned resource, and a readable diff. ``POST
/api/v1/recommendations/{id}/manual/applied`` ("Mark as applied") records the change as
applied by hand with a change id, so the change-measurement pipeline measures it like a
merged pull request (C2). Nothing here writes to the cluster: Git, or the operator, is
the only write path.
"""

from __future__ import annotations

import json
import shlex
import uuid
from datetime import datetime, timezone
from typing import Any, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from greenkube.api.dependencies import (
    get_authenticated_actor,
    get_gitops_binding_repository,
    get_recommendation_repository,
)
from greenkube.automation.actions import ACTION_KINDS, ActionKindError, PrimitiveApplyError, action_kind_of
from greenkube.automation.actions.delete_resource import deletion_target_for, describe_target
from greenkube.automation.actions.primitives import strategic_merge_patch
from greenkube.automation.actions.workloads import ACTION_API_VERSIONS, POD_SPEC_PATHS
from greenkube.core.optimization.applied_detector import K8sResourceReader, ResourceReader, ResourceReadError
from greenkube.gitops.contracts import ActionStatus, PlanStatus
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.storage.base_repository import RecommendationRepository

router = APIRouter()

#: Action statuses with no pull request in flight (a preview plan may exist).
_NO_PR_IN_FLIGHT = frozenset(
    {None, ActionStatus.NONE, ActionStatus.GROUPED, ActionStatus.PLAN_READY, ActionStatus.FAILED, ActionStatus.CLOSED}
)
#: Plan statuses the automation delivers or is delivering: "Mark as applied" would race it.
_DELIVERING_PLAN_STATUSES = frozenset(
    {PlanStatus.APPROVED, PlanStatus.APPLYING, PlanStatus.PR_OPEN, PlanStatus.MERGED, PlanStatus.SYNCED}
)


def get_manual_resource_reader() -> ResourceReader:
    """Reads the live resource a manual deletion targets (overridable in tests)."""
    return K8sResourceReader()


#: ``kubectl`` resource names of the kinds manual mode addresses.
_KUBECTL_RESOURCES = {
    "CronJob": "cronjob",
    "DaemonSet": "daemonset",
    "Deployment": "deployment",
    "Job": "job",
    "PersistentVolume": "persistentvolume",
    "ReplicaSet": "replicaset",
    "ReplicationController": "replicationcontroller",
    "Service": "service",
    "StatefulSet": "statefulset",
}


class ManualCommand(BaseModel):
    """One command to run by hand, in order."""

    description: str
    command: str


class ManualApplyGuide(BaseModel):
    """How to apply a recommendation without GitOps, and how it will be measured."""

    recommendation_id: int
    mode: Literal["patch", "delete", "advisory"] = Field(
        description="patch: kubectl patch; delete: kubectl delete; advisory: no snippet, apply by hand."
    )
    action_kind: Optional[str] = None
    target: Optional[dict] = Field(default=None, description="apiVersion, kind, namespace and name of the resource.")
    commands: List[ManualCommand] = Field(default_factory=list)
    patch: Optional[dict] = Field(default=None, description="The patch document of a typed change.")
    diff: Optional[str] = Field(default=None, description="A readable diff of the change.")
    notes: List[str] = Field(default_factory=list)
    can_mark_applied: bool = Field(description="Whether 'Mark as applied' is available (active recommendations).")


def _status(record: RecommendationRecord) -> str:
    return str(getattr(record.status, "value", record.status) or "")


def _q(value: str) -> str:
    return shlex.quote(value)


def _namespace_flag(namespace: str) -> str:
    return f" -n {_q(namespace)}" if namespace else ""


def _quantity(resource: str, value: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    return f"{value}m" if resource == "cpu" else str(value)


def _rightsizing_diff(record: RecommendationRecord) -> Optional[str]:
    if record.type == RecommendationType.RIGHTSIZING_CPU:
        resource, current, proposed = (
            "cpu",
            record.current_cpu_request_millicores,
            record.recommended_cpu_request_millicores,
        )
    elif record.type == RecommendationType.RIGHTSIZING_MEMORY:
        resource, current, proposed = (
            "memory",
            record.current_memory_request_bytes,
            record.recommended_memory_request_bytes,
        )
    else:
        return None
    before, after = _quantity(resource, current), _quantity(resource, proposed)
    if before is None or after is None:
        return None
    owner = f"{record.owner_kind}/{record.owner_name}" if record.owner_kind and record.owner_name else record.pod_name
    container = record.container_name or "?"
    return (
        f"--- live {owner} (container {container})\n"
        f"+++ proposed\n"
        f"@@ resources.requests @@\n"
        f"-{resource}: {before}\n"
        f"+{resource}: {after}\n"
    )


def manual_guide(record: RecommendationRecord) -> ManualApplyGuide:
    """The manual-mode guide of one recommendation (pure; never reads the cluster)."""
    assert record.id is not None
    active = _status(record) == RecommendationStatus.ACTIVE.value
    notes = [
        "Mark the recommendation as applied once the change is live: GreenKube then measures it like a merged "
        "pull request (baseline before, measured window after)."
    ]
    deletion = deletion_target_for(record)
    if deletion is not None:
        resource = _KUBECTL_RESOURCES[deletion.kind]
        base = f"kubectl delete {resource} {_q(deletion.name)}{_namespace_flag(deletion.namespace)}"
        commands = [
            ManualCommand(
                description="Check what would be deleted (server-side dry run).", command=f"{base} --dry-run=server"
            ),
            ManualCommand(description=f"Delete {describe_target(deletion)}.", command=base),
        ]
        notes.insert(
            0,
            "Deleting cannot be undone. Check the evidence in the recommendation and confirm with the owners first; "
            'annotate the resource with greenkube.io/ignore="true" instead if it is still needed.',
        )
        notes.append(
            "Mark as applied only after the delete: GreenKube reads the resource first and records the change only "
            "once the cluster answers that it is gone."
        )
        if deletion.kind == "PersistentVolume":
            notes.insert(
                1,
                "With reclaimPolicy Retain the cloud disk survives the PersistentVolume: release it in the cloud "
                "console or CLI too, or its cost continues.",
            )
        return ManualApplyGuide(
            recommendation_id=record.id,
            mode="delete",
            action_kind="delete_resource",
            target=deletion.model_dump(mode="json"),
            commands=commands,
            notes=notes,
            can_mark_applied=active,
        )

    action = record.action
    spec = action_kind_of(action) if action is not None else None
    patch: Optional[dict] = None
    if action is not None and spec is not None and spec.kind in ACTION_KINDS:
        try:
            patch = strategic_merge_patch(spec.compile(action.target, action.operations)) or None
        except (ActionKindError, PrimitiveApplyError):
            patch = None
    if patch is not None and action is not None:
        target = action.target
        resource = _KUBECTL_RESOURCES.get(target.kind, target.kind.lower())
        patch_type = "strategic" if target.kind in POD_SPEC_PATHS else "merge"
        body = json.dumps(patch, separators=(",", ":"), sort_keys=True)
        namespace = _namespace_flag(target.namespace)
        command = f"kubectl patch {resource} {_q(target.name)}{namespace} --type {patch_type} -p {_q(body)}"
        commands = [
            ManualCommand(
                description="Preview the change (server-side dry run).",
                command=f"{command} --dry-run=server -o yaml",
            ),
            ManualCommand(description=f"Apply it to {target.kind} {target.name}.", command=command),
        ]
        if target.kind in POD_SPEC_PATHS:
            commands.append(
                ManualCommand(
                    description="Wait for the rollout to complete.",
                    command=f"kubectl rollout status {resource} {_q(target.name)}{namespace}",
                )
            )
        notes.append(
            "If a GitOps controller or Helm release manages this workload, it reverts a manual patch: change the "
            "source in Git instead."
        )
        return ManualApplyGuide(
            recommendation_id=record.id,
            mode="patch",
            action_kind=spec.kind if spec is not None else None,
            target=target.model_dump(mode="json"),
            commands=commands,
            patch=patch,
            diff=_rightsizing_diff(record),
            notes=notes,
            can_mark_applied=active,
        )

    target_dict: Optional[dict] = None
    if record.owner_kind and record.owner_name and record.namespace:
        target_dict = {
            "api_version": ACTION_API_VERSIONS.get(record.owner_kind, ""),
            "kind": record.owner_kind,
            "namespace": record.namespace,
            "name": record.owner_name,
        }
    notes.insert(0, "GreenKube has no command for this kind of change: apply it by hand as the description says.")
    return ManualApplyGuide(
        recommendation_id=record.id,
        mode="advisory",
        target=target_dict,
        diff=_rightsizing_diff(record),
        notes=notes,
        can_mark_applied=active,
    )


@router.get("/recommendations/{rec_id}/manual", response_model=ManualApplyGuide)
async def get_manual_guide(
    rec_id: int,
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
) -> ManualApplyGuide:
    """The commands and diff to apply a recommendation by hand (manual mode, H9)."""
    record = await reco_repo.get_recommendation_by_id(rec_id)
    if record is None or record.id is None:
        raise HTTPException(status_code=404, detail=f"Recommendation {rec_id} not found.")
    return manual_guide(record)


@router.post("/recommendations/{rec_id}/manual/applied", response_model=RecommendationRecord)
async def mark_applied_manually(
    rec_id: int,
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
    actor: str = Depends(get_authenticated_actor),
    resource_reader: ResourceReader = Depends(get_manual_resource_reader),
    gitops_repo: Any = Depends(get_gitops_binding_repository),
) -> Any:
    """Mark an active recommendation as applied by hand ("Mark as applied", H9).

    The record gets a change id, so the change-measurement pipeline records the change
    and measures it like a merged pull request. Only ``active`` recommendations: one with
    an open pull request is applied by its merge.

    A deletion is only marked applied once the live cluster says the resource is absent
    (409 while it still exists, 503 when it cannot be read: an unreadable resource is
    never taken for an absent one), so no saving is ever claimed for a resource that is
    still there. A recommendation with a plan in flight (approved, pull request open,
    merged or syncing) is refused: the automation applies it.
    """
    from greenkube.core.optimization.lifecycle import RecommendationLifecycle, RecommendationTransitionError

    record = await reco_repo.get_recommendation_by_id(rec_id)
    if record is None or record.id is None:
        raise HTTPException(status_code=404, detail=f"Recommendation {rec_id} not found.")
    if _status(record) != RecommendationStatus.ACTIVE.value:
        raise HTTPException(
            status_code=409,
            detail=f"Recommendation {rec_id} is {_status(record)}; only active recommendations can be marked applied.",
        )
    try:
        action_status = await gitops_repo.get_recommendation_action_status(rec_id)
        delivering = (
            []
            if action_status not in _NO_PR_IN_FLIGHT
            else [
                plan
                for plan in await gitops_repo.list_plans_for_recommendation(rec_id)
                if plan.status in _DELIVERING_PLAN_STATUSES
            ]
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Could not check whether a plan is in flight for this recommendation."
        ) from exc
    if action_status not in _NO_PR_IN_FLIGHT or delivering:
        state = getattr(action_status, "value", action_status) if not delivering else delivering[0].status.value
        raise HTTPException(
            status_code=409,
            detail=(
                f"Recommendation {rec_id} has a plan in flight ({state}); close the plan or its pull request "
                "before applying it by hand."
            ),
        )
    rollout: Optional[dict] = None
    deletion = deletion_target_for(record)
    if deletion is not None:
        try:
            live = await resource_reader.read_resource(deletion)
        except ResourceReadError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Could not check that {describe_target(deletion)} is gone: {exc}. Try again later.",
            ) from exc
        if live is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{describe_target(deletion)} still exists. Delete it first: the recommendation is marked "
                    "applied once the resource is absent."
                ),
            )
        rollout = {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "completion_source": "resource_absent",
        }
    actual_cpu = (
        record.recommended_cpu_request_millicores if record.type == RecommendationType.RIGHTSIZING_CPU else None
    )
    actual_memory = (
        record.recommended_memory_request_bytes if record.type == RecommendationType.RIGHTSIZING_MEMORY else None
    )
    try:
        return await RecommendationLifecycle(reco_repo).apply(
            rec_id,
            actual_cpu=actual_cpu,
            actual_memory=actual_memory,
            application_method="manual",
            actor=actor,
            change_id=uuid.uuid4().hex,
            rollout=rollout,
        )
    except RecommendationTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
