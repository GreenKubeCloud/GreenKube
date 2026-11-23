# src/greenkube/demo/automation.py
"""GitOps automation for the demo: a configured install with pull requests at every stage.

The demo has no cluster, no Git server and no controller. This module records
what a configured install would hold, through the production repositories and
state machines:

* a Gitea provider (``git.greenoptic.example``, fictional) with two allowlisted
  repositories, one synced by Argo CD and one by Flux, and the workload bindings
  GitOps discovery would find (one ambiguous, two unresolved);
* one automation plan per delivered change, rendered by the production raw-manifest
  planner from a synthetic manifest (real diff, digest and validation), then walked
  through the plan and action status transitions the plan worker, the pull-request
  lifecycle, the sync observer and the apply detector perform;
* the pull-request records of those plans (open, merged and syncing, merged and
  rolled out, closed unmerged);
* the readiness snapshot, worker and controller heartbeats the setup checklist and
  the automation status read, kept fresh by :class:`DemoLiveState` while the demo runs.

The plan service the API gets (:class:`DemoPlanService`) only reads: previews,
approvals and rollbacks are refused, so no visitor can change the shared demo.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, cast

from greenkube.automation.plans import AutomationPlanError
from greenkube.demo.changes import (
    DEMO_CHANGES,
    DEMO_CONTAINER,
    DEMO_GIT_HOST,
    MERGE_LEAD,
    ROLLOUT_DURATION,
    SYNC_LEAD,
    merge_sha,
    pull_request_url,
    rollout_completed_at,
)
from greenkube.gitops.contracts import (
    ActionPlanV3,
    ActionStatus,
    AutomationErrorCode,
    BindingStatus,
    ContainerResources,
    ControllerKind,
    Operation,
    Plan,
    PlanStatus,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    WorkloadSource,
)
from greenkube.models.metrics import (
    PullRequestRecord,
    PullRequestStatus,
    RecommendationEvent,
    RecommendationEventType,
    RecommendationPatchV2,
    RecommendationRecord,
    RecommendationStatus,
    RecommendationType,
)
from greenkube.storage.base_gitops_repository import GitOpsProviderConfig, WorkloadBinding

logger = logging.getLogger(__name__)

DEMO_PROVIDER_ID = "greenoptic-gitea"
DEMO_PROVIDER_TYPE = "gitea"
DEMO_API_BASE_URL = f"https://{DEMO_GIT_HOST}/api/v1"
DEMO_CREDENTIALS_SECRET = "greenkube-git-credentials"
DEMO_CREDENTIALS_KEY = "token"
PLATFORM_REPO = "greenoptic/platform-gitops"
MANUFACTURING_REPO = "greenoptic/manufacturing-apps"
#: The worker identity the demo plan operations were executed by (the controller runs plans since M8).
DEMO_WORKER_ID = "greenkube-controller-0"
#: Pending previews expire after the production plan TTL (``AutomationPlanService.plan_ttl``).
PLAN_TTL = timedelta(minutes=30)

#: Who approved the demo plans (fictional platform engineers).
_APPROVERS = ("user:camille.roux", "user:noah.lefevre", "user:ines.garnier")


def repository_url(repository: str) -> str:
    """Clone URL of a demo repository."""
    return f"https://{DEMO_GIT_HOST}/{repository}.git"


def _sha1(*parts: object) -> str:
    return hashlib.sha1("|".join(str(part) for part in parts).encode()).hexdigest()


def _blob_sha(content: str) -> str:
    """The Git blob id of ``content`` (what the provider reports for the file)."""
    data = content.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# --------------------------------------------------------------------------- workload sources


@dataclass(frozen=True)
class DemoWorkload:
    """A demo workload as GitOps discovery sees it: its source, or why there is none."""

    namespace: str
    name: str
    kind: str = "Deployment"
    repository: str = PLATFORM_REPO
    controller: ControllerKind = ControllerKind.ARGOCD
    status: BindingStatus = BindingStatus.RESOLVED
    reason: Optional[str] = None
    replicas: int = 2
    cpu_m: Optional[int] = None
    memory_bytes: Optional[int] = None

    @property
    def controller_name(self) -> str:
        return self.namespace if self.controller is ControllerKind.ARGOCD else "factory-apps"

    @property
    def controller_namespace(self) -> str:
        return "argocd" if self.controller is ControllerKind.ARGOCD else "flux-system"

    @property
    def path(self) -> str:
        return f"apps/{self.namespace}/{self.name}"

    @property
    def manifest_path(self) -> str:
        return f"{self.path}/deployment.yaml"


MIB = 1024 * 1024

DEMO_WORKLOADS: tuple[DemoWorkload, ...] = (
    DemoWorkload("commerce", "checkout-api-eu", replicas=4, cpu_m=600, memory_bytes=768 * MIB),
    DemoWorkload("commerce", "checkout-api-us", replicas=4, cpu_m=600, memory_bytes=768 * MIB),
    DemoWorkload("commerce", "pricing-service", replicas=1, cpu_m=900, memory_bytes=512 * MIB),
    DemoWorkload("website", "storefront-web-eu", replicas=3, cpu_m=700, memory_bytes=768 * MIB),
    DemoWorkload("website", "storefront-web-us", replicas=3, cpu_m=700, memory_bytes=768 * MIB),
    DemoWorkload("platform", "api-gateway", replicas=1, cpu_m=1200, memory_bytes=768 * MIB),
    DemoWorkload("platform", "identity-service", replicas=2, cpu_m=400, memory_bytes=512 * MIB),
    DemoWorkload("platform", "notification-service", replicas=2, cpu_m=300, memory_bytes=384 * MIB),
    DemoWorkload("platform", "catalog-search", replicas=3, cpu_m=500, memory_bytes=1024 * MIB),
    DemoWorkload("retail", "stock-reservation", replicas=2, cpu_m=350, memory_bytes=512 * MIB),
    DemoWorkload("retail", "pos-sync", replicas=2, cpu_m=400, memory_bytes=512 * MIB),
    DemoWorkload("superset", "superset-worker", replicas=2, cpu_m=600, memory_bytes=1024 * MIB),
    DemoWorkload(
        "manufacturing",
        "lens-configurator",
        repository=MANUFACTURING_REPO,
        controller=ControllerKind.FLUX_KUSTOMIZATION,
        replicas=1,
        cpu_m=400,
        memory_bytes=1024 * MIB,
    ),
    DemoWorkload(
        "manufacturing",
        "factory-scheduler",
        repository=MANUFACTURING_REPO,
        controller=ControllerKind.FLUX_KUSTOMIZATION,
        replicas=1,
        cpu_m=450,
        memory_bytes=512 * MIB,
    ),
    # Listed by two Flux Kustomizations of equal priority: a person must choose the source.
    DemoWorkload(
        "manufacturing",
        "qa-traceability",
        repository=MANUFACTURING_REPO,
        controller=ControllerKind.FLUX_KUSTOMIZATION,
        status=BindingStatus.AMBIGUOUS,
        reason="conflicting-equal-priority-source-units",
        cpu_m=350,
        memory_bytes=512 * MIB,
    ),
    # Installed by hand (Helm CLI) long ago: no Argo CD Application or Flux object manages them.
    DemoWorkload(
        "minio", "minio", kind="StatefulSet", status=BindingStatus.UNRESOLVED, reason="workload-source-unresolved"
    ),
    DemoWorkload(
        "dremio",
        "dremio-coordinator",
        kind="StatefulSet",
        status=BindingStatus.UNRESOLVED,
        reason="workload-source-unresolved",
    ),
)

_WORKLOADS_BY_KEY = {(item.namespace, item.name): item for item in DEMO_WORKLOADS}


def _source(
    workload: DemoWorkload,
    *,
    cluster: str,
    revision: str,
    observed_at: datetime,
    controller_name: Optional[str] = None,
    path: Optional[str] = None,
) -> WorkloadSource:
    controller = controller_name or workload.controller_name
    return WorkloadSource(
        id=f"src-{_sha1(cluster, workload.controller.value, controller, workload.namespace, workload.name)[:20]}",
        cluster=cluster,
        namespace=workload.namespace,
        workload_kind=workload.kind,
        workload_name=workload.name,
        provider_id=DEMO_PROVIDER_ID,
        repo_url=repository_url(workload.repository),
        ref="refs/heads/main",
        revision=revision,
        path=path or workload.path,
        controller_kind=workload.controller,
        controller_name=controller,
        controller_namespace=workload.controller_namespace,
        controller_uid=_sha1("uid", controller)[:36],
        controller_generation=12,
        source_revision=revision,
        renderer=RendererKind.RAW,
        confidence=0.97,
        status=BindingStatus.RESOLVED,
        # The plan service copies the binding's policy evidence onto its source; discovery stores both.
        evidence={
            "controller": "application" if workload.controller is ControllerKind.ARGOCD else "kustomization",
            **_binding_evidence(workload),
        },
        observed_at=observed_at,
    )


def _binding_evidence(workload: DemoWorkload) -> dict[str, Any]:
    """What discovery records about the workload for the automation policy gates."""
    return {
        "hpa_pdb_conflict": False,
        "pdb_conflict": False,
        "workload_labels": {"app.kubernetes.io/name": workload.name, "app.kubernetes.io/part-of": workload.namespace},
        "rollout_in_progress": False,
        "controller_suspended": False,
        "controller_healthy": True,
        "hpa_pdb_interactions_checked": True,
        "hpa_targets": [],
        "vpa_interactions_checked": True,
        "vpa_targets": [],
    }


def head_revision(repository: str) -> str:
    """The current head of a demo repository's main branch."""
    return _sha1("head", repository)


def build_bindings(cluster: str, observed_at: datetime) -> list[WorkloadBinding]:
    """The workload bindings GitOps discovery reports for the demo cluster."""
    bindings: list[WorkloadBinding] = []
    for workload in DEMO_WORKLOADS:
        revision = head_revision(workload.repository)
        common = {
            "cluster": cluster,
            "namespace": workload.namespace,
            "workload_kind": workload.kind,
            "workload_name": workload.name,
            "observed_at": observed_at,
        }
        if workload.status is BindingStatus.RESOLVED:
            source = _source(workload, cluster=cluster, revision=revision, observed_at=observed_at)
            bindings.append(
                WorkloadBinding(
                    status=BindingStatus.RESOLVED,
                    confidence=source.confidence,
                    source=source,
                    evidence=_binding_evidence(workload),
                    **common,
                )
            )
        elif workload.status is BindingStatus.AMBIGUOUS:
            candidates = tuple(
                _source(
                    workload,
                    cluster=cluster,
                    revision=revision,
                    observed_at=observed_at,
                    controller_name=controller,
                    path=path,
                ).model_copy(update={"status": BindingStatus.AMBIGUOUS, "confidence": 0.5})
                for controller, path in (
                    ("factory-apps", workload.path),
                    ("factory-legacy", f"legacy/{workload.namespace}/{workload.name}"),
                )
            )
            bindings.append(
                WorkloadBinding(
                    status=BindingStatus.AMBIGUOUS,
                    confidence=0.5,
                    candidates=candidates,
                    reason=workload.reason,
                    evidence=_binding_evidence(workload),
                    **common,
                )
            )
        else:
            bindings.append(
                WorkloadBinding(status=workload.status, confidence=0.0, reason=workload.reason, evidence={}, **common)
            )
    return bindings


def manifest(workload: DemoWorkload, *, cpu_m: Optional[int], memory_bytes: Optional[int]) -> str:
    """The workload's Deployment as stored in Git, with the given container requests."""
    from greenkube.utils.k8s_quantities import format_cpu_quantity, format_memory_quantity

    requests = []
    if cpu_m is not None:
        requests.append(f"              cpu: {format_cpu_quantity(cpu_m)}")
    if memory_bytes is not None:
        requests.append(f"              memory: {format_memory_quantity(memory_bytes)}")
    limit = format_memory_quantity(max(memory_bytes or 0, 512 * MIB) * 2)
    image = f"registry.{DEMO_GIT_HOST.split('.', 1)[1]}/{workload.namespace}/{workload.name}"
    return "\n".join(
        [
            "apiVersion: apps/v1",
            f"kind: {workload.kind}",
            "metadata:",
            f"  name: {workload.name}",
            f"  namespace: {workload.namespace}",
            "  labels:",
            f"    app.kubernetes.io/name: {workload.name}",
            f"    app.kubernetes.io/part-of: {workload.namespace}",
            "spec:",
            f"  replicas: {workload.replicas}",
            "  selector:",
            "    matchLabels:",
            f"      app.kubernetes.io/name: {workload.name}",
            "  template:",
            "    metadata:",
            "      labels:",
            f"        app.kubernetes.io/name: {workload.name}",
            "    spec:",
            "      containers:",
            f"        - name: {DEMO_CONTAINER}",
            f"          image: {image}:{1 + len(workload.name) % 4}.{len(workload.namespace)}.2",
            "          ports:",
            "            - containerPort: 8080",
            "          resources:",
            "            requests:",
            *requests,
            "            limits:",
            f"              memory: {limit}",
            "",
        ]
    )


# --------------------------------------------------------------------------- deliveries


@dataclass(frozen=True)
class DemoDelivery:
    """One recommendation delivered (or being delivered) through an automation plan and its pull request.

    A delivery whose recommendation was rolled out takes its timeline from the
    recommendation's ``applied_at``; the others give theirs relative to now.
    """

    namespace: str
    owner_name: str
    status: PlanStatus
    pr_number: Optional[int] = None
    #: Review time between the pull request being opened and merged.
    review: timedelta = timedelta(days=1)
    #: In-flight deliveries: how long ago the plan was created and its PR opened, merged, synced or closed.
    created_ago: Optional[timedelta] = None
    opened_ago: Optional[timedelta] = None
    merged_ago: Optional[timedelta] = None
    synced_ago: Optional[timedelta] = None
    closed_ago: Optional[timedelta] = None
    #: What the plan proposes when it differs from the recommendation's own value (none in the demo).
    approver: str = _APPROVERS[0]
    #: Why the team ignored the recommendation after closing the pull request.
    ignored_reason: Optional[str] = None

    @property
    def workload(self) -> DemoWorkload:
        return _WORKLOADS_BY_KEY[(self.namespace, self.owner_name)]

    @property
    def repository(self) -> str:
        return self.workload.repository


DEMO_DELIVERIES: tuple[DemoDelivery, ...] = (
    # A preview a platform engineer opened a few minutes ago: validated, waiting for approval.
    DemoDelivery("commerce", "checkout-api-eu", PlanStatus.VALIDATED, created_ago=timedelta(minutes=9)),
    # Approved two days ago; the pull request waits for the search team's review.
    DemoDelivery(
        "platform",
        "catalog-search",
        PlanStatus.PR_OPEN,
        pr_number=431,
        opened_ago=timedelta(days=2, hours=3),
        approver=_APPROVERS[1],
    ),
    # Merged 25 minutes ago; Argo CD has not synced the merge commit yet.
    DemoDelivery(
        "website",
        "storefront-web-eu",
        PlanStatus.MERGED,
        pr_number=436,
        opened_ago=timedelta(days=1, hours=2),
        merged_ago=timedelta(minutes=25),
    ),
    # Merged and synced; the Deployment is rolling out.
    DemoDelivery(
        "commerce",
        "checkout-api-us",
        PlanStatus.SYNCED,
        pr_number=437,
        opened_ago=timedelta(hours=20),
        merged_ago=timedelta(minutes=50),
        synced_ago=timedelta(minutes=12),
        approver=_APPROVERS[2],
    ),
    # Rolled out: warm-up, measuring, and measured outcomes.
    DemoDelivery("retail", "stock-reservation", PlanStatus.APPLIED, pr_number=435, review=timedelta(hours=20)),
    DemoDelivery("platform", "notification-service", PlanStatus.APPLIED, pr_number=429, approver=_APPROVERS[1]),
    DemoDelivery("commerce", "pricing-service", PlanStatus.APPLIED, pr_number=412, review=timedelta(days=3)),
    DemoDelivery("platform", "api-gateway", PlanStatus.APPLIED, pr_number=398, review=timedelta(days=6)),
    DemoDelivery("retail", "pos-sync", PlanStatus.APPLIED, pr_number=419, approver=_APPROVERS[2]),
    DemoDelivery("manufacturing", "lens-configurator", PlanStatus.APPLIED, pr_number=57, review=timedelta(days=2)),
    DemoDelivery("manufacturing", "factory-scheduler", PlanStatus.APPLIED, pr_number=61, approver=_APPROVERS[1]),
    # Closed unmerged by the finance team, who then ignored the recommendation.
    DemoDelivery(
        "superset",
        "superset-worker",
        PlanStatus.CLOSED,
        pr_number=421,
        opened_ago=timedelta(days=15),
        closed_ago=timedelta(days=12, hours=5),
        approver=_APPROVERS[2],
        ignored_reason="Superset workers spike at the month-end close; the finance team keeps the headroom.",
    ),
)

#: Plan status path of each delivery stage, in the order the production components walk it.
PLAN_PATH: tuple[PlanStatus, ...] = (
    PlanStatus.VALIDATED,
    PlanStatus.APPROVED,
    PlanStatus.APPLYING,
    PlanStatus.PR_OPEN,
    PlanStatus.MERGED,
    PlanStatus.SYNCED,
    PlanStatus.APPLIED,
)
#: Who moves a plan into each status (actor of the audit event), and the action status it sets.
_STEP_ACTOR = {
    PlanStatus.APPLYING: "automation-worker",
    PlanStatus.PR_OPEN: "automation-worker",
    PlanStatus.MERGED: "git-provider",
    PlanStatus.SYNCED: "gitops-sync-observer",
    PlanStatus.APPLIED: "apply-detector",
    PlanStatus.CLOSED: "git-provider",
}
_STEP_ACTION = {
    PlanStatus.PR_OPEN: (ActionStatus.PR_OPEN, (ActionStatus.PLAN_READY, ActionStatus.PR_OPEN)),
    PlanStatus.MERGED: (ActionStatus.MERGED, (ActionStatus.PR_OPEN,)),
    PlanStatus.SYNCED: (ActionStatus.SYNC_PENDING, (ActionStatus.MERGED,)),
    PlanStatus.APPLIED: (ActionStatus.APPLIED, (ActionStatus.SYNC_PENDING,)),
    PlanStatus.CLOSED: (ActionStatus.CLOSED, (ActionStatus.PR_OPEN,)),
}


def plan_path(target: PlanStatus) -> list[PlanStatus]:
    """The statuses a plan passes through to reach ``target`` (``closed`` leaves from ``pr_open``)."""
    if target is PlanStatus.CLOSED:
        return [*PLAN_PATH[: PLAN_PATH.index(PlanStatus.PR_OPEN) + 1], PlanStatus.CLOSED]
    return list(PLAN_PATH[: PLAN_PATH.index(target) + 1])


@dataclass
class DeliveryTimeline:
    """When each step of a delivery happened."""

    created: datetime
    approved: Optional[datetime] = None
    opened: Optional[datetime] = None
    merged: Optional[datetime] = None
    synced: Optional[datetime] = None
    rolled_out: Optional[datetime] = None
    closed: Optional[datetime] = None

    def at(self, status: PlanStatus) -> datetime:
        moments = {
            PlanStatus.VALIDATED: self.created,
            PlanStatus.APPROVED: self.approved,
            PlanStatus.APPLYING: self.approved + timedelta(seconds=40) if self.approved else None,
            PlanStatus.PR_OPEN: self.opened,
            PlanStatus.MERGED: self.merged,
            PlanStatus.SYNCED: self.synced,
            PlanStatus.APPLIED: self.rolled_out,
            PlanStatus.CLOSED: self.closed,
        }
        moment = moments.get(status)
        if moment is None:
            raise ValueError(f"the delivery timeline has no time for {status.value}")
        return moment


def delivery_timeline(delivery: DemoDelivery, record: RecommendationRecord, now: datetime) -> DeliveryTimeline:
    """The delivery's timeline: from the rollout backwards when it landed, else from now."""

    def ago(value: Optional[timedelta]) -> Optional[datetime]:
        return now - value if value is not None else None

    if record.applied_at is not None:
        rolled_out = rollout_completed_at(record.applied_at)
        merged = rolled_out - MERGE_LEAD
        opened = merged - delivery.review
        return DeliveryTimeline(
            created=opened - timedelta(minutes=8),
            approved=opened - timedelta(minutes=3),
            opened=opened,
            merged=merged,
            synced=rolled_out - ROLLOUT_DURATION - SYNC_LEAD,
            rolled_out=rolled_out,
        )
    opened = ago(delivery.opened_ago)
    created = ago(delivery.created_ago) or (opened - timedelta(minutes=8) if opened else now)
    return DeliveryTimeline(
        created=created,
        approved=opened - timedelta(minutes=3) if opened else None,
        opened=opened,
        merged=ago(delivery.merged_ago),
        synced=ago(delivery.synced_ago),
        closed=ago(delivery.closed_ago),
    )


def container_action(record: RecommendationRecord) -> Optional[ActionPlanV3]:
    """The typed container-resources action of a demo rightsizing, as the analyzers store it."""
    from greenkube.utils.k8s_quantities import format_cpu_quantity, format_memory_quantity

    if record.id is None or not record.owner_kind or not record.owner_name or not record.container_name:
        return None
    if record.type is RecommendationType.RIGHTSIZING_CPU and record.recommended_cpu_request_millicores:
        requests = {"cpu": format_cpu_quantity(record.recommended_cpu_request_millicores)}
    elif record.type is RecommendationType.RIGHTSIZING_MEMORY and record.recommended_memory_request_bytes:
        requests = {"memory": format_memory_quantity(record.recommended_memory_request_bytes)}
    else:
        return None
    target = ResourceTarget(
        api_version="apps/v1", kind=record.owner_kind, namespace=record.namespace or "", name=record.owner_name
    )
    operations = [
        SetContainerResources(container=record.container_name, resources=ContainerResources(requests=requests))
    ]
    payload = {
        "target": target.model_dump(mode="json"),
        "recommendation_ids": [record.id],
        "operations": [operation.model_dump(mode="json") for operation in operations],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return ActionPlanV3(
        action_id=f"action-{digest[:20]}",
        recommendation_ids=[record.id],
        target=target,
        operations=list(operations),
        constraints={"container_targeted": True},
        evidence_ref=f"recommendation-evidence/{record.id}",
    )


def typed_action(record: RecommendationRecord) -> Optional[ActionPlanV3]:
    """The typed action a scan stores on the recommendation (rightsizing or deletion)."""
    if record.type in (RecommendationType.ORPHANED_LOAD_BALANCER, RecommendationType.ORPHANED_PERSISTENT_VOLUME):
        from greenkube.automation.actions.delete_resource import DeleteResourceSpec

        # A stored recommendation is the recommendation the deletion spec reads (same fields).
        return DeleteResourceSpec().ops_for([cast(Any, record)]).get(record.id or 0)
    return container_action(record)


def build_plan(
    delivery: DemoDelivery,
    record: RecommendationRecord,
    action: ActionPlanV3,
    *,
    cluster: str,
    created_at: datetime,
) -> Plan:
    """The validated plan of a delivery, rendered by the production raw-manifest planner."""
    from greenkube.gitops.planning.raw_manifest import RepositorySnapshot, plan_raw_manifest

    workload = delivery.workload
    before = manifest(
        workload,
        cpu_m=record.current_cpu_request_millicores or workload.cpu_m,
        memory_bytes=record.current_memory_request_bytes or workload.memory_bytes,
    )
    base_revision = _sha1("base", delivery.repository, delivery.pr_number or delivery.owner_name)
    source = _source(workload, cluster=cluster, revision=base_revision, observed_at=created_at)
    snapshot = RepositorySnapshot({workload.manifest_path: before}, {workload.manifest_path: _blob_sha(before)})
    file_plan = plan_raw_manifest(action, source, snapshot, base_revision)
    return Plan(
        plan_id=file_plan.plan_id,
        recommendation_ids=list(action.recommendation_ids),
        source=source,
        action=action,
        file_plan=file_plan,
        status=PlanStatus.VALIDATED,
        digest=file_plan.digest,
        created_at=created_at,
        expires_at=created_at + PLAN_TTL,
    )


def pull_request_record(
    delivery: DemoDelivery, plan: Plan, record: RecommendationRecord, timeline: DeliveryTimeline
) -> Optional[PullRequestRecord]:
    """The pull-request record of a delivery once its plan opened one."""
    if delivery.pr_number is None or timeline.opened is None:
        return None
    from greenkube.automation.plan_worker import plan_branch_name

    status = {
        PlanStatus.PR_OPEN: PullRequestStatus.OPEN,
        PlanStatus.CLOSED: PullRequestStatus.CLOSED,
    }.get(delivery.status, PullRequestStatus.MERGED)
    merged = status is PullRequestStatus.MERGED
    merge_commit = merge_sha(delivery.repository, delivery.pr_number) if merged else None
    sync_status: Optional[str] = None
    sync_observed_at: Optional[datetime] = None
    sync_error_code: Optional[str] = None
    if merged and timeline.synced is not None:
        sync_status, sync_observed_at = "synced", timeline.synced
    elif merged:
        # Merged, not synced yet: the sync observer saw the controller still reconciling.
        sync_status = "pending"
        sync_observed_at = (timeline.merged or timeline.opened) + timedelta(minutes=2)
        sync_error_code = "reconciliation_pending"
    updated = max(
        moment for moment in (timeline.opened, timeline.merged, timeline.synced, timeline.closed) if moment is not None
    )
    return PullRequestRecord(
        recommendation_id=record.id or 0,
        provider=DEMO_PROVIDER_TYPE,
        repo=delivery.repository,
        base_branch="main",
        head_branch=plan_branch_name(plan),
        pr_number=delivery.pr_number,
        pr_url=pull_request_url(delivery.repository, delivery.pr_number),
        status=status,
        created_at=timeline.opened,
        updated_at=updated,
        plan_id=plan.plan_id,
        provider_id=DEMO_PROVIDER_ID,
        commit_sha=_sha1("head", plan.plan_id),
        merge_commit_sha=merge_commit,
        merged_at=timeline.merged if merged else None,
        closed_at=timeline.closed if status is PullRequestStatus.CLOSED else (timeline.merged if merged else None),
        sync_status=sync_status,
        sync_revision=merge_commit if sync_status == "synced" else None,
        sync_observed_at=sync_observed_at,
        sync_error_code=sync_error_code,
    )


# --------------------------------------------------------------------------- population


@dataclass
class DemoAutomationResult:
    """What the automation step recorded: plans by recommendation id, and counts."""

    plan_ids: dict[int, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)


def demo_cluster() -> str:
    from greenkube.core.config import get_config

    return get_config().CLUSTER_NAME or "default"


async def _all_records(reco_repo) -> list[RecommendationRecord]:
    statuses = [status.value for status in RecommendationStatus]
    return list(await reco_repo.get_recommendations_by_statuses(statuses))


def _find(records: Iterable[RecommendationRecord], namespace: str, owner_name: str) -> Optional[RecommendationRecord]:
    return next(
        (record for record in records if record.namespace == namespace and record.owner_name == owner_name),
        None,
    )


async def seed_provider(gitops_repo, *, now: datetime) -> None:
    """The Gitea provider Helm values declare, verified, with its repository allowlist."""
    await gitops_repo.save_provider_config(
        GitOpsProviderConfig(
            id=DEMO_PROVIDER_ID,
            provider_type=DEMO_PROVIDER_TYPE,
            api_base_url=DEMO_API_BASE_URL,
            secret_name=DEMO_CREDENTIALS_SECRET,
            secret_key=DEMO_CREDENTIALS_KEY,
            enabled=True,
            capabilities={"pull_requests": True, "labels": True, "comments": True, "commit_status": True},
            status="verified",
            last_checked_at=now,
        )
    )
    for repository in (PLATFORM_REPO, MANUFACTURING_REPO):
        await gitops_repo.add_provider_allowlist(DEMO_PROVIDER_ID, repository)


async def save_bindings(gitops_repo, *, cluster: str, now: datetime) -> int:
    """(Re)record the bindings discovery reports, observed at ``now``."""
    saved = 0
    for binding in build_bindings(cluster, now):
        current = await gitops_repo.get_workload_binding(
            binding.cluster, binding.namespace, binding.workload_kind, binding.workload_name
        )
        generation = current.generation if current is not None else 0
        await gitops_repo.save_workload_binding(_with_generation(binding, generation + 1))
        saved += 1
    return saved


def _with_generation(binding: WorkloadBinding, generation: int) -> WorkloadBinding:
    from dataclasses import replace

    return replace(binding, generation=generation)


async def _event_watermark(db_manager) -> int:
    async with db_manager.connection_scope() as conn:
        cursor = await conn.execute("SELECT COALESCE(MAX(id), 0) FROM recommendation_events")
        row = await cursor.fetchone()
    return int(row[0] or 0)


def _event_time(event_type: str, details: Mapping[str, Any], timeline: DeliveryTimeline) -> Optional[datetime]:
    """When the audit event a repository wrote during the walk really happened."""
    if event_type in ("plan_created", "plan_policy_evaluated"):
        return timeline.created
    if event_type == "plan_status_changed":
        try:
            return timeline.at(PlanStatus(details.get("new_status")))
        except ValueError:
            return None
    if event_type == "plan_action_status_changed":
        by_action = {
            "plan_ready": timeline.created,
            "pr_open": timeline.opened,
            "merged": timeline.merged,
            "sync_pending": timeline.synced,
            "applied": timeline.rolled_out,
            "closed": timeline.closed,
        }
        return by_action.get(str(details.get("new_action_status")))
    if event_type == RecommendationEventType.PR_OPENED.value:
        return timeline.opened
    if event_type == RecommendationEventType.PR_CLOSED.value:
        return timeline.closed
    return None


async def _backdate_events(
    db_manager, since_id: int, timelines: Mapping[int, DeliveryTimeline], to_iso: Callable[[datetime], str]
) -> int:
    """Give the audit events written by the walk the time their step happened.

    The repositories stamp their events with the wall clock; the demo walks
    months of deliveries in a second, so each event gets its step's time.
    """
    async with db_manager.connection_scope() as conn:
        cursor = await conn.execute(
            "SELECT id, recommendation_id, event_type, payload FROM recommendation_events WHERE id > ?", (since_id,)
        )
        rows = await cursor.fetchall()
        updated = 0
        for row in rows:
            timeline = timelines.get(int(row[1]))
            if timeline is None:
                continue
            try:
                payload = json.loads(row[3] or "{}")
            except ValueError:
                payload = {}
            details = payload.get("details") if isinstance(payload, dict) else None
            moment = _event_time(str(row[2]), details if isinstance(details, dict) else {}, timeline)
            if moment is None:
                continue
            await conn.execute("UPDATE recommendation_events SET created_at = ? WHERE id = ?", (to_iso(moment), row[0]))
            updated += 1
        await conn.commit()
    return updated


async def _record(reco_repo, rec_id: int, event: RecommendationEventType, at: datetime, actor: str, **details) -> None:
    await reco_repo.record_event(
        RecommendationEvent(
            recommendation_id=rec_id,
            event_type=event.value,
            actor=actor,
            payload={"details": details},
            created_at=at,
        )
    )


async def _lifecycle_events(
    reco_repo,
    delivery: DemoDelivery,
    record: RecommendationRecord,
    plan: Plan,
    timeline: DeliveryTimeline,
    now: datetime,
) -> None:
    """Recommendation lifecycle events the pull-request lifecycle and the measurement write."""
    rec_id = record.id or 0
    if record.applied_at is not None and timeline.opened is not None:
        # Rolled-out recommendations were saved with their final status: their PR history is replayed here.
        await _record(reco_repo, rec_id, RecommendationEventType.PR_OPENED, timeline.opened, "automation-worker")
    if timeline.merged is not None:
        await _record(
            reco_repo,
            rec_id,
            RecommendationEventType.PR_MERGED,
            timeline.merged,
            "git-provider",
            plan_id=plan.plan_id,
            pr_number=delivery.pr_number,
        )
    if record.applied_at is None or timeline.rolled_out is None:
        return
    await _record(
        reco_repo,
        rec_id,
        RecommendationEventType.APPLIED,
        timeline.rolled_out,
        "detector",
        application_method="pr_merge",
        plan_id=plan.plan_id,
    )
    status = record.status.value if hasattr(record.status, "value") else str(record.status)
    measuring_from = timeline.rolled_out + timedelta(hours=24)
    if status == RecommendationStatus.APPLIED.value and record.verification_status == "pending":
        return
    await _record(reco_repo, rec_id, RecommendationEventType.VERIFICATION_STARTED, measuring_from, "change-measurement")
    decided = record.verified_at or (timeline.rolled_out + timedelta(days=8))
    outcome = {
        "passed": RecommendationEventType.VERIFIED,
        "failed": RecommendationEventType.NOT_REALIZED,
        "inconclusive": RecommendationEventType.INCONCLUSIVE,
    }.get(str(record.verification_status))
    if outcome is not None:
        await _record(reco_repo, rec_id, outcome, decided, "change-measurement")
    if status == RecommendationStatus.REGRESSED.value:
        spec = next((item for item in DEMO_CHANGES if item.owner_name == delivery.owner_name), None)
        if spec is not None and spec.regressed_days_ago is not None:
            await _record(
                reco_repo,
                rec_id,
                RecommendationEventType.REGRESSED,
                now - timedelta(days=spec.regressed_days_ago),
                "savings-drift",
                reason=spec.regressed_reason,
            )


async def _walk_plan(
    gitops_repo,
    operations,
    plan: Plan,
    delivery: DemoDelivery,
) -> Plan:
    """Save the plan and move it, and its recommendations' action status, along the delivery path."""
    saved = await gitops_repo.save_plan_if_absent(plan, actor=delivery.approver)
    await gitops_repo.set_recommendation_action_status(
        saved.recommendation_ids,
        ActionStatus.PLAN_READY,
        expected_statuses=(ActionStatus.NONE, ActionStatus.GROUPED, ActionStatus.CLOSED, ActionStatus.FAILED),
        actor=delivery.approver,
        plan_id=saved.plan_id,
    )
    path = plan_path(delivery.status)
    current = saved
    for previous, status in zip(path, path[1:]):
        updated = await gitops_repo.set_plan_status(
            current.plan_id,
            status,
            expected_statuses=(previous,),
            expected_digest=current.digest,
            actor=_STEP_ACTOR.get(status, delivery.approver),
        )
        if updated is None:
            raise RuntimeError(f"demo plan {current.plan_id}: {previous.value} -> {status.value} was refused")
        current = updated
        if status is PlanStatus.APPROVED:
            await gitops_repo.set_plan_delivery_mode(
                current.plan_id, "direct", expected_digest=current.digest, expected_statuses=(PlanStatus.APPROVED,)
            )
            await _run_operation(operations, current, delivery)
        step = _STEP_ACTION.get(status)
        if step is not None:
            action_status, expected = step
            count = await gitops_repo.set_recommendation_action_status(
                current.recommendation_ids,
                action_status,
                expected_statuses=expected,
                actor=_STEP_ACTOR[status],
                plan_id=current.plan_id,
            )
            if count != len(current.recommendation_ids):
                raise RuntimeError(f"demo plan {current.plan_id}: action status {action_status.value} was refused")
    return current


async def _run_operation(operations, plan: Plan, delivery: DemoDelivery) -> None:
    """Queue the approved plan's operation and complete it as the plan worker does."""
    await operations.enqueue(plan, idempotency_key=f"plan:{plan.plan_id}:{plan.digest}")
    lease = await operations.claim_next(DEMO_WORKER_ID)
    if lease is None:
        raise RuntimeError(f"demo plan {plan.plan_id}: its operation could not be claimed")
    await operations.mark_succeeded(
        lease,
        provider_id=DEMO_PROVIDER_ID,
        commit_sha=_sha1("head", plan.plan_id),
        pull_request_number=delivery.pr_number,
    )


async def populate_demo_automation(
    reco_repo,
    gitops_repo,
    operations,
    pr_repo,
    db_manager,
    *,
    now: Optional[datetime] = None,
    cluster: Optional[str] = None,
) -> DemoAutomationResult:
    """Record the configured install and every demo delivery; returns the plans by recommendation."""
    from greenkube.core.optimization.lifecycle import RecommendationLifecycle
    from greenkube.utils.time import to_iso_z

    current = now or datetime.now(timezone.utc)
    cluster_name = cluster or demo_cluster()
    result = DemoAutomationResult()
    await seed_provider(gitops_repo, now=current)
    result.counts["workload_bindings"] = await save_bindings(gitops_repo, cluster=cluster_name, now=current)

    records = await _all_records(reco_repo)
    # Every rightsizing and deletion carries its typed action, as a scan stores it.
    actions: dict[int, ActionPlanV3] = {}
    for record in records:
        action = typed_action(record) if record.id is not None else None
        if action is None or record.id is None:
            continue
        actions[record.id] = action
        await reco_repo.update_recommendation_fields(
            record.id, {"patch": RecommendationPatchV2(action=action).model_dump(mode="json")}
        )

    watermark = await _event_watermark(db_manager)
    timelines: dict[int, DeliveryTimeline] = {}
    lifecycle = RecommendationLifecycle(reco_repo)
    pull_requests = 0
    for delivery in DEMO_DELIVERIES:
        record = _find(records, delivery.namespace, delivery.owner_name)
        if record is None or record.id is None or record.id not in actions:
            logger.warning("Demo delivery %s/%s skipped: no recommendation.", delivery.namespace, delivery.owner_name)
            continue
        timeline = delivery_timeline(delivery, record, current)
        plan = build_plan(delivery, record, actions[record.id], cluster=cluster_name, created_at=timeline.created)
        plan = await _walk_plan(gitops_repo, operations, plan, delivery)
        timelines[record.id] = timeline
        result.plan_ids[record.id] = plan.plan_id
        pr = pull_request_record(delivery, plan, record, timeline)
        if pr is not None:
            await pr_repo.save_pull_request(pr)
            pull_requests += 1
        await _lifecycle_events(reco_repo, delivery, record, plan, timeline, current)
        if delivery.ignored_reason and timeline.closed is not None:
            ignored_at = timeline.closed + timedelta(hours=2)
            await lifecycle.transition(
                record.id,
                RecommendationStatus.IGNORED,
                event_type=RecommendationEventType.IGNORED,
                updates={"ignored_at": ignored_at, "ignored_reason": delivery.ignored_reason, "updated_at": ignored_at},
                actor=delivery.approver,
                payload={"details": {"reason": delivery.ignored_reason}},
                expected_statuses=[RecommendationStatus.ACTIVE],
            )
            timelines[record.id] = timeline
    result.counts["backdated_events"] = await _backdate_events(db_manager, watermark, timelines, to_iso_z)
    await _backdate_ignored(db_manager, watermark, timelines, to_iso_z)
    result.counts["automation_plans"] = len(result.plan_ids)
    result.counts["pull_requests"] = pull_requests
    return result


async def _backdate_ignored(
    db_manager, since_id: int, timelines: Mapping[int, DeliveryTimeline], to_iso: Callable[[datetime], str]
) -> None:
    """The ``ignored`` event of a closed delivery happened two hours after the pull request was closed."""
    async with db_manager.connection_scope() as conn:
        for rec_id, timeline in timelines.items():
            if timeline.closed is None:
                continue
            await conn.execute(
                "UPDATE recommendation_events SET created_at = ? "
                "WHERE id > ? AND recommendation_id = ? AND event_type = ?",
                (to_iso(timeline.closed + timedelta(hours=2)), since_id, rec_id, RecommendationEventType.IGNORED.value),
            )
        await conn.commit()


# --------------------------------------------------------------------------- read-only plan service


def _demo_refusal() -> AutomationPlanError:
    return AutomationPlanError(
        AutomationErrorCode.PROVIDER_NOT_READY,
        "the demo has no Git provider: plans are read-only and pull requests are disabled",
    )


class DemoPlanService:
    """The demo's plan service: reads the recorded plans, refuses every preview, approval or rollback."""

    #: No plan worker: the delivery routes of patch and ci modes answer 503 in the demo.
    worker = None

    def __init__(self, plans, operations) -> None:
        self._plans = plans
        self._operations = operations

    async def read_plan(self, plan_id: str) -> Optional[Plan]:
        return await self._plans.get_plan(plan_id)

    async def read_operation(self, operation_id: str) -> Optional[Operation]:
        return await self._operations.get_by_operation_id(operation_id)

    async def read_operation_for_plan(self, plan_id: str) -> Optional[Operation]:
        return await self._operations.get_by_plan_id(plan_id)

    async def prepare_plan(
        self, recommendation_ids: Sequence[int], *, actor: str = "system", values_mapping: Any = None
    ) -> Plan:
        raise _demo_refusal()

    async def preview_plan(self, recommendation_ids: Sequence[int]) -> Plan:
        raise _demo_refusal()

    async def prepare_rollback(self, plan_id: str) -> Plan:
        raise _demo_refusal()

    async def approve_plan(self, plan_id: str, digest: str, *, actor: str = "system") -> Plan:
        raise _demo_refusal()

    async def mark_stale(self, plan_id: str, *, actor: str = "system", reason_code: Optional[str] = None) -> None:
        raise _demo_refusal()

    async def cancel_by_user(self, plan_id: str, *, actor: str = "system") -> None:
        raise _demo_refusal()

    async def expire_due_plans(self) -> list[Plan]:
        return []

    async def prune_retained_history(self, *, cutoff: datetime) -> dict[str, int]:
        return {}


# --------------------------------------------------------------------------- live state


class _DemoCredentials:
    """Credentials inspector of the demo: the provider's token Secret is mounted (a placeholder)."""

    resolver = None

    async def refresh(self, secret_name: str) -> None:
        return None

    def default_credentials(self, secret_name: str) -> Optional[str]:
        return None

    def default_credentials_host(self, secret_name: str) -> Optional[str]:
        return None

    def provider_has_credentials(self, config: GitOpsProviderConfig) -> bool:
        return config.id == DEMO_PROVIDER_ID and config.secret_name is not None


_DISCOVERY = {
    "argocd": {"available": True, "reason": None},
    "flux_kustomization": {"available": True, "reason": None},
    "flux_helm_release": {"available": True, "reason": None},
}

_CONTROLLER_LOOPS = (
    ("collection", 300.0),
    ("recommendation-scan", 3600.0),
    ("gitops-discovery", 300.0),
    ("automation-readiness", 60.0),
    ("gitops-sync", 60.0),
    ("plan-executor", 30.0),
    ("savings-attribution", 3600.0),
    ("ledger-chain", 3600.0),
)


class DemoLiveState:
    """Keeps the demo's live signals fresh: readiness snapshot, heartbeats, bindings and the pending preview.

    In a real install the controller refreshes them every minute; without one they
    would look stale a few minutes after the demo starts.
    """

    def __init__(self, db_manager, gitops_repo, operations, *, cluster: Optional[str] = None) -> None:
        self._db = db_manager
        self._gitops = gitops_repo
        self._operations = operations
        self._cluster = cluster or demo_cluster()
        self._started_at = datetime.now(timezone.utc)
        self._runs = 0

    async def refresh(self, now: Optional[datetime] = None) -> None:
        current = now or datetime.now(timezone.utc)
        self._runs += 1
        await save_bindings(self._gitops, cluster=self._cluster, now=current)
        await self._readiness(current)
        # The plan worker's heartbeat: a claim that finds no queued operation.
        await self._operations.claim_next(DEMO_WORKER_ID)
        await self._controller_heartbeat(current)
        await self._renew_pending_previews(current)

    async def _readiness(self, now: datetime) -> None:
        from types import SimpleNamespace

        from greenkube.automation.readiness import AutomationReadiness
        from greenkube.core.config import DISCOVERED_REPOSITORIES
        from greenkube.storage.automation_capability_repository import AutomationCapabilityRepository

        async def renderer_status() -> Mapping[str, Any]:
            return {"egress": {"enforced": True, "checked_at": now.isoformat()}}

        readiness = AutomationReadiness(
            gitops_repository=self._gitops,
            capability_store=AutomationCapabilityRepository(self._db),
            cluster=self._cluster,
            config=SimpleNamespace(
                GITOPS_DEFAULT_CREDENTIALS_SECRET="",
                automation_repository_allowlist=(DISCOVERED_REPOSITORIES,),
                automation_delivery_paused=False,
                AUTOMATION_DELIVERY_MODE="direct",
            ),
            credentials=_DemoCredentials(),  # type: ignore[arg-type]
            discovery_status=lambda: _DISCOVERY,
            renderer_status=renderer_status,
            webhooks_configured=lambda: True,
            clock=lambda: now,
        )
        await readiness.run_once()

    async def _controller_heartbeat(self, now: datetime) -> None:
        from greenkube.core.loops import LoopStatus
        from greenkube.storage.controller_heartbeat_repository import (
            ControllerHeartbeat,
            ControllerHeartbeatRepository,
        )

        loops = []
        for name, interval in _CONTROLLER_LOOPS:
            status = LoopStatus(
                name=name,
                leader_only=name != "collection",
                interval_seconds=interval,
                timeout_seconds=interval,
                stale_after_seconds=interval * 2 + 60,
                state="sleeping",
                heartbeat_at=now,
                last_started_at=now - timedelta(seconds=2),
                last_finished_at=now - timedelta(seconds=1),
                last_success_at=now - timedelta(seconds=1),
                last_duration_seconds=0.8,
                last_outcome="success",
                runs=self._runs,
            )
            loops.append(status.snapshot(now))
        await ControllerHeartbeatRepository(self._db).publish(
            ControllerHeartbeat(
                instance_id=DEMO_WORKER_ID,
                role="controller",
                leader=True,
                ready=True,
                heartbeat_at=now,
                started_at=self._started_at,
                loops=loops,
            )
        )

    async def _renew_pending_previews(self, now: datetime) -> None:
        """A pending preview is re-rendered before it expires, as reopening the recommendation does."""
        for plan in await self._gitops.list_plans(status=PlanStatus.VALIDATED):
            if plan.expires_at is not None and plan.expires_at - now > PLAN_TTL / 2:
                continue
            created = now - timedelta(minutes=9)
            await self._gitops.save_plan(
                plan.model_copy(update={"created_at": created, "expires_at": created + PLAN_TTL})
            )

    async def run(self, interval_seconds: float = 30.0) -> None:
        """Refresh until cancelled; a failed refresh is logged and retried."""
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("Demo live state refresh failed (%s).", type(error).__name__)
            await asyncio.sleep(interval_seconds)
