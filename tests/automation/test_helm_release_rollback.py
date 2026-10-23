from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from ruamel.yaml import YAML

from greenkube.automation.plan_runtime import RuntimePlanService
from greenkube.automation.plans import AutomationPlanService, GitBlobFile
from greenkube.automation.rollback import (
    FluxHelmReleaseRollbackRenderer,
    RollbackConflictError,
    RollbackValidationError,
    UnsupportedRollbackError,
    build_inverse_plan,
)
from greenkube.gitops.contracts import (
    ActionPlanV3,
    BindingStatus,
    ContainerRequestBaseline,
    ContainerResources,
    ControllerKind,
    Plan,
    PlanStatus,
    PreApplyWorkloadBaseline,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    WorkloadSource,
)
from greenkube.gitops.planning.helm import FluxHelmReleasePlanner
from greenkube.gitops.renderers.raw import RawRenderResult, render_raw
from greenkube.gitops.snapshots import RepositorySnapshot, snapshot_from_files
from greenkube.models.evidence import RecommendationEvidence
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.storage.base_gitops_repository import WorkloadBinding

BASE_REVISION = "a" * 40
CURRENT_REVISION = "b" * 40
PROVIDER_ID = "github-main"
REPO_URL = "https://github.com/acme/gitops.git"
RELEASE_PATH = "clusters/prod/helmrelease.yaml"
TARGET = ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="prod", name="payments-api")
_CHART = {
    "charts/payments/Chart.yaml": "apiVersion: v2\nname: payments\nversion: 1.0.0\n",
    "charts/payments/values.yaml": """\
app:
  resources:
    requests:
      cpu: 100m
      memory: 128Mi
    limits:
      cpu: 500m
      memory: 512Mi
""",
}


def _yaml_load(content: str) -> Any:
    parser = YAML(typ="safe")
    parser.allow_duplicate_keys = False
    return parser.load(content)


def _yaml_dump(value: Any) -> str:
    stream = io.StringIO()
    serializer = YAML(typ="safe")
    serializer.default_flow_style = False
    serializer.sort_base_mapping_type_on_output = False
    serializer.dump(value, stream)
    return stream.getvalue()


def _merge(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(left)
    for key, value in right.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


class _FluxRenderer:
    def __init__(self, *, unrelated: bool = False) -> None:
        self.unrelated = unrelated

    def render(
        self,
        snapshot: RepositorySnapshot,
        chart_path: str,
        *,
        release_name: str,
        release_namespace: str,
        values_files: Sequence[str] = (),
    ) -> RawRenderResult:
        values_path = snapshot.workspace / chart_path / "values.yaml"
        values = _yaml_load(values_path.read_text(encoding="utf-8")) or {}
        for path in values_files:
            overlay = _yaml_load((snapshot.workspace / path).read_text(encoding="utf-8")) or {}
            values = _merge(values, overlay)
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": TARGET.name, "namespace": TARGET.namespace},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": "api", "resources": values["app"]["resources"]},
                            {
                                "name": "sidecar",
                                "resources": {"requests": {"cpu": "25m", "memory": "64Mi"}},
                            },
                        ]
                    }
                }
            },
        }
        del release_name, release_namespace
        sources = {"rendered/flux-helm-release.yaml": _yaml_dump(manifest)}
        if self.unrelated:
            sources["rendered/unrelated.yaml"] = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: newly-rendered
  namespace: prod
data:
  value: changed
"""
        return replace(render_raw(sources), renderer=RendererKind.HELM_RELEASE)


def _action() -> ActionPlanV3:
    return ActionPlanV3(
        action_id="act-1",
        recommendation_ids=[42],
        target=TARGET,
        operations=[
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"cpu": "300m", "memory": "256Mi"}),
            )
        ],
        evidence_ref="recommendation/42",
    )


def _source() -> WorkloadSource:
    return WorkloadSource(
        id="source-flux-helm",
        cluster="prod-cluster",
        namespace="prod",
        workload_kind="Deployment",
        workload_name="payments-api",
        provider_id=PROVIDER_ID,
        repo_url=REPO_URL,
        ref="main",
        revision=BASE_REVISION,
        path="clusters/prod",
        controller_kind=ControllerKind.FLUX_HELM_RELEASE,
        controller_name="payments",
        controller_namespace="flux-system",
        source_revision=f"main@sha1:{BASE_REVISION}",
        renderer=RendererKind.HELM_RELEASE,
        renderer_config={
            "chart": {
                "name": "./charts/payments",
                "version": None,
                "repo_url": REPO_URL,
                "source_kind": "GitRepository",
            },
            "release_name": "payments",
            "release_namespace": "prod",
            "values": {},
            "values_from": [],
            "post_renderers": [],
            "manifest_source": {
                "provider_id": PROVIDER_ID,
                "repo_url": REPO_URL,
                "ref": "main",
                "path": "clusters/prod",
                "revision": BASE_REVISION,
            },
        },
        evidence={
            "source_ref": {"kind": "GitRepository", "name": "gitops", "namespace": "flux-system"},
            "artifact_revision": f"main@sha1:{BASE_REVISION}",
        },
        confidence=0.99,
        status=BindingStatus.RESOLVED,
    )


def _release_manifest() -> str:
    return """\
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: payments
  namespace: flux-system
spec:
  releaseName: payments
  targetNamespace: prod
  chart:
    spec:
      chart: ./charts/payments
      sourceRef:
        kind: GitRepository
        name: gitops
        namespace: flux-system
  values:
    app:
      resources:
        requests:
          cpu: 100m
          memory: 128Mi
"""


def _snapshot(files: dict[str, str], revision: str = BASE_REVISION) -> RepositorySnapshot:
    return snapshot_from_files(
        files,
        provider_id=PROVIDER_ID,
        repo_url=REPO_URL,
        commit_sha=revision,
        workspace_parent=Path.cwd(),
    )


def _helm_planner(*, unrelated: bool = False) -> FluxHelmReleasePlanner:
    return FluxHelmReleasePlanner(renderer=_FluxRenderer(unrelated=unrelated))


def _baseline(*, api_cpu: int = 100) -> PreApplyWorkloadBaseline:
    return PreApplyWorkloadBaseline(
        workload_uid="uid-payments-api",
        generation=11,
        observed_generation=11,
        container_requests={
            "api": ContainerRequestBaseline(cpu_millicores=api_cpu, memory_bytes=128 * 1024 * 1024),
            "sidecar": ContainerRequestBaseline(cpu_millicores=25, memory_bytes=64 * 1024 * 1024),
        },
    )


def _forward_plan(
    *,
    planner: FluxHelmReleasePlanner | None = None,
    baseline: PreApplyWorkloadBaseline | None = None,
) -> tuple[Plan, FluxHelmReleasePlanner, str, str]:
    source = _source()
    original_release = _release_manifest()
    selected_planner = planner or _helm_planner()
    snapshot = _snapshot({**_CHART, RELEASE_PATH: original_release})
    try:
        file_plan = selected_planner.plan(_action(), source, snapshot, BASE_REVISION)
    finally:
        snapshot.close()
    forward_release = file_plan.files[0].content
    assert forward_release is not None
    plan = Plan(
        plan_id=file_plan.plan_id,
        recommendation_ids=_action().recommendation_ids,
        source=source,
        action=_action(),
        file_plan=file_plan,
        status=PlanStatus.SYNCED,
        digest=file_plan.digest,
        pre_apply_baseline=baseline or _baseline(),
    )
    return plan, selected_planner, original_release, forward_release


def _current_source(plan: Plan, *, revision: str = CURRENT_REVISION) -> WorkloadSource:
    assert plan.file_plan is not None
    forward_content = plan.file_plan.files[0].content
    assert forward_content is not None
    forward_release = _yaml_load(forward_content)
    source = plan.source
    config = {
        **source.renderer_config,
        "manifest_source": {
            **source.renderer_config["manifest_source"],
            "revision": revision,
        },
        "post_renderers": forward_release["spec"].get("postRenderers", []),
    }
    evidence = {
        **source.evidence,
        "artifact_revision": f"main@sha1:{revision}",
    }
    return source.model_copy(
        update={
            "revision": revision,
            "source_revision": f"main@sha1:{revision}",
            "renderer_config": config,
            "evidence": evidence,
        }
    )


def _snapshot_at(files: dict[str, str], revision: str) -> RepositorySnapshot:
    return _snapshot(files, revision)


def _build_inverse(
    plan: Plan,
    planner: FluxHelmReleasePlanner,
    original_release: str,
    forward_release: str,
    *,
    current_source: WorkloadSource | None = None,
    current_files: dict[str, str] | None = None,
) -> tuple[Any, RepositorySnapshot]:
    source = current_source or _current_source(plan)
    snapshot = _snapshot_at(
        current_files or {**_CHART, RELEASE_PATH: forward_release},
        CURRENT_REVISION,
    )
    renderer = FluxHelmReleaseRollbackRenderer(
        planner,
        plan.action,
        RELEASE_PATH,
        workspace_parent=Path.cwd(),
    )
    inverse = build_inverse_plan(
        plan,
        {RELEASE_PATH: original_release},
        snapshot,
        CURRENT_REVISION,
        renderer=renderer,
        current_source=source,
    )
    return inverse, snapshot


def test_flux_helm_release_rollback_restores_the_exact_manifest_and_preapply_container_baseline() -> None:
    plan, planner, original_release, forward_release = _forward_plan()
    inverse, snapshot = _build_inverse(plan, planner, original_release, forward_release)

    try:
        file_plan = plan.file_plan
        assert file_plan is not None
        assert inverse.renderer is RendererKind.HELM_RELEASE
        assert [(edit.path, edit.operation, edit.content) for edit in inverse.files] == [
            (RELEASE_PATH, "update", original_release),
        ]
        assert inverse.files[0].original_sha is not None
        assert inverse.rendered_before_digest == file_plan.validation.output_digest
        assert inverse.rendered_before_digest != inverse.rendered_after_digest
        assert snapshot.metadata.commit_sha == CURRENT_REVISION
    finally:
        snapshot.close()


def test_flux_helm_release_rollback_rejects_a_candidate_that_does_not_match_the_preapply_baseline() -> None:
    plan, planner, original_release, forward_release = _forward_plan(baseline=_baseline(api_cpu=90))
    snapshot = _snapshot_at({**_CHART, RELEASE_PATH: forward_release}, CURRENT_REVISION)
    renderer = FluxHelmReleaseRollbackRenderer(
        planner,
        plan.action,
        RELEASE_PATH,
        workspace_parent=Path.cwd(),
    )

    try:
        with pytest.raises(RollbackValidationError, match="baseline"):
            build_inverse_plan(
                plan,
                {RELEASE_PATH: original_release},
                snapshot,
                CURRENT_REVISION,
                renderer=renderer,
                current_source=_current_source(plan),
            )
    finally:
        snapshot.close()


def test_flux_helm_release_rollback_rejects_a_source_artifact_from_another_revision() -> None:
    plan, planner, original_release, forward_release = _forward_plan()
    stale_source = _current_source(plan).model_copy(
        update={
            "source_revision": f"main@sha1:{BASE_REVISION}",
            "evidence": {
                **_current_source(plan).evidence,
                "artifact_revision": f"main@sha1:{BASE_REVISION}",
            },
        }
    )
    snapshot = _snapshot_at({**_CHART, RELEASE_PATH: forward_release}, CURRENT_REVISION)
    renderer = FluxHelmReleaseRollbackRenderer(
        planner,
        plan.action,
        RELEASE_PATH,
        workspace_parent=Path.cwd(),
    )

    try:
        with pytest.raises(UnsupportedRollbackError, match="same-revision"):
            build_inverse_plan(
                plan,
                {RELEASE_PATH: original_release},
                snapshot,
                CURRENT_REVISION,
                renderer=renderer,
                current_source=stale_source,
            )
    finally:
        snapshot.close()


def test_flux_helm_release_rollback_rejects_unrelated_rendered_changes() -> None:
    plan, _, original_release, forward_release = _forward_plan()
    source = _current_source(plan)
    chart_files = {
        **_CHART,
        RELEASE_PATH: forward_release,
        "charts/payments/templates/configmap.yaml": "apiVersion: v1\nkind: ConfigMap\n",
    }

    rollback_planner = _helm_planner(unrelated=True)
    snapshot = _snapshot_at(chart_files, CURRENT_REVISION)
    renderer = FluxHelmReleaseRollbackRenderer(
        rollback_planner,
        plan.action,
        RELEASE_PATH,
        workspace_parent=Path.cwd(),
    )

    try:
        with pytest.raises(RollbackConflictError, match="forward plan output"):
            build_inverse_plan(
                plan,
                {RELEASE_PATH: original_release},
                snapshot,
                CURRENT_REVISION,
                renderer=renderer,
                current_source=source,
            )
    finally:
        snapshot.close()


@pytest.mark.asyncio
async def test_flux_helm_release_runtime_persists_a_revision_bound_rollback_preview() -> None:
    original_plan, planner, original_release, forward_release = _forward_plan()
    current_source = _current_source(original_plan)
    original_file_plan = original_plan.file_plan
    assert original_file_plan is not None
    recommendation = RecommendationRecord(
        id=original_plan.recommendation_ids[0],
        container_name="api",
        namespace=TARGET.namespace,
        owner_kind=TARGET.kind,
        owner_name=TARGET.name,
        type=RecommendationType.RIGHTSIZING_CPU,
        description="Reduce CPU requests.",
        scope="workload",
        status=RecommendationStatus.ROLLBACK_REVIEW,
        action=original_plan.action,
        risk_level="low",
        confidence=0.9,
        evidence=RecommendationEvidence(sample_count=36, coverage_ratio=1.0),
    )

    class Recommendations:
        async def get_recommendation_by_id(self, recommendation_id: int) -> RecommendationRecord | None:
            return recommendation if recommendation_id == recommendation.id else None

    class Bindings:
        async def get_workload_binding(
            self,
            cluster: str,
            namespace: str,
            workload_kind: str,
            workload_name: str,
        ) -> WorkloadBinding:
            assert (cluster, namespace, workload_kind, workload_name) == (
                current_source.cluster,
                TARGET.namespace,
                TARGET.kind,
                TARGET.name,
            )
            return WorkloadBinding(
                cluster=cluster,
                namespace=namespace,
                workload_kind=workload_kind,
                workload_name=workload_name,
                status=current_source.status,
                confidence=current_source.confidence,
                source=current_source,
            )

    class Snapshots:
        created: RepositorySnapshot | None = None

        async def create_snapshot(self, source: WorkloadSource) -> RepositorySnapshot:
            assert source.id == current_source.id
            self.created = _snapshot_at({**_CHART, RELEASE_PATH: forward_release}, CURRENT_REVISION)
            return self.created

    class Blobs:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def get_blob_file(
            self,
            source: WorkloadSource,
            path: str,
            revision: str,
        ) -> GitBlobFile | None:
            assert source.id == current_source.id
            self.calls.append((path, revision))
            edit = original_file_plan.files[0]
            return GitBlobFile(path=path, content=original_release, blob_sha=edit.original_sha or "")

    class PlanRepository:
        def __init__(self) -> None:
            self.plans = {original_plan.plan_id: original_plan}

        async def get_plan(self, plan_id: str) -> Plan | None:
            return self.plans.get(plan_id)

        async def set_plan_policy_result(
            self,
            plan_id: str,
            policy_result: dict[str, Any],
            *,
            expected_digest: str,
            expected_statuses: Sequence[PlanStatus] | None = None,
            actor: str = "system",
        ) -> Plan | None:
            del actor
            plan = self.plans.get(plan_id)
            if (
                plan is None
                or plan.digest != expected_digest
                or (expected_statuses is not None and plan.status not in expected_statuses)
            ):
                return None
            updated = plan.model_copy(update={"policy_result": policy_result})
            self.plans[plan_id] = updated
            return updated

        async def list_plans(
            self,
            *,
            status: PlanStatus | None = None,
            source_id: str | None = None,
        ) -> list[Plan]:
            return [
                plan
                for plan in self.plans.values()
                if (status is None or plan.status is status) and (source_id is None or plan.source.id == source_id)
            ]

        async def save_plan_if_absent(self, plan: Plan, **kwargs: Any) -> Plan:
            del kwargs
            return self.plans.setdefault(plan.plan_id, plan)

        async def set_plan_status(
            self,
            plan_id: str,
            status: PlanStatus,
            *,
            expected_statuses: Sequence[PlanStatus] | None = None,
            expected_digest: str | None = None,
            actor: str = "system",
            reason_code: str | None = None,
        ) -> Plan | None:
            plan = self.plans.get(plan_id)
            if plan is None:
                return None
            updated = plan.model_copy(update={"status": status})
            self.plans[plan_id] = updated
            return updated

    class RevisionResolver:
        async def resolve_current_revision(self, source: WorkloadSource) -> str:
            assert source.id == current_source.id
            return CURRENT_REVISION

    class Queue:
        def __init__(self) -> None:
            self.keys: list[str] = []

        async def enqueue(self, plan: Plan, *, idempotency_key: str) -> None:
            assert plan.rollback_of_plan_id == original_plan.plan_id
            self.keys.append(idempotency_key)

    snapshots = Snapshots()
    repository = PlanRepository()
    blobs = Blobs()
    queue = Queue()
    plans = AutomationPlanService(
        cast(Any, repository),
        git_blob_resolver=blobs,
        revision_resolver=cast(Any, RevisionResolver()),
        planners={RendererKind.HELM_RELEASE: planner},
        operation_enqueuer=cast(Any, queue),
    )
    runtime = RuntimePlanService(
        recommendation_repository=cast(Any, Recommendations()),
        binding_repository=cast(Any, Bindings()),
        cluster=current_source.cluster,
        snapshot_resolver=cast(Any, snapshots),
        plan_service=cast(Any, plans),
        snapshot_workspace_parent=Path.cwd(),
    )

    preview = await runtime.prepare_rollback(original_plan.plan_id)

    assert preview.rollback_of_plan_id == original_plan.plan_id
    assert preview.status is PlanStatus.VALIDATED
    assert preview.file_plan is not None
    assert preview.file_plan.base_revision == CURRENT_REVISION
    assert preview.source.source_revision == CURRENT_REVISION
    assert preview.file_plan.files[0].content == original_release
    assert blobs.calls == [(RELEASE_PATH, BASE_REVISION)]
    assert snapshots.created is not None and not snapshots.created.workspace.exists()

    approved = await runtime.approve_plan(preview.plan_id, preview.digest or "")

    assert approved.status is PlanStatus.APPROVED
    assert approved.rollback_of_plan_id == original_plan.plan_id
    assert queue.keys == [f"plan:{preview.plan_id}:{preview.digest}"]
    assert repository.plans[preview.plan_id].status is PlanStatus.APPROVED
