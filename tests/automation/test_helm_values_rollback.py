from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest
from ruamel.yaml import YAML

from greenkube.automation.plan_runtime import RuntimePlanService
from greenkube.automation.plans import AutomationPlanError, AutomationPlanService, GitBlobFile
from greenkube.automation.rollback import (
    ONE_TIME_HELM_VALUES_MAPPING_EVIDENCE_KEY,
    HelmValuesRollbackRenderer,
    RollbackConflictError,
    UnsupportedRollbackError,
    build_inverse_plan,
    same_rollback_source,
)
from greenkube.gitops.contracts import (
    ActionPlanV3,
    AutomationErrorCode,
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
from greenkube.gitops.planning.helm import HelmValuesPlanner
from greenkube.gitops.renderers.helm import HelmRenderer
from greenkube.gitops.renderers.sandbox import SandboxResult
from greenkube.gitops.snapshots import RepositorySnapshot, snapshot_from_files
from greenkube.models.evidence import RecommendationEvidence
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.storage.base_gitops_repository import WorkloadBinding

BASE_REVISION = "a" * 40
CURRENT_REVISION = "b" * 40
PROVIDER_ID = "github-main"
REPO_URL = "https://github.com/acme/gitops.git"
VALUES_PATH = "apps/payments/values.yaml"
MAPPING = {
    "file_path": VALUES_PATH,
    "container": "api",
    "path": ["app", "resources"],
}
CHART_FILES = {
    "charts/payments/Chart.yaml": "apiVersion: v2\nname: payments\nversion: 1.0.0\n",
    "charts/payments/values.yaml": """\
app:
  resources:
    requests:
      cpu: 50m
      memory: 64Mi
""",
    "charts/payments/templates/deployment.yaml": """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: payments
spec:
  template:
    spec:
      containers:
        - name: api
          resources:
            {{- toYaml .Values.app.resources | nindent 12 }}
""",
}
ORIGINAL_VALUES = """\
# Keep this user-owned value; rollback targets the observed baseline instead.
app:
  resources:
    requests:
      cpu: 80m
      memory: 128Mi
"""


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


class _HelmRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        workspace: Path,
        cwd: Path,
        timeout_seconds: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> SandboxResult:
        del cwd, timeout_seconds, max_stdout_bytes, max_stderr_bytes
        if tuple(argv[1:]) == ("version", "--short"):
            return SandboxResult(returncode=0, stdout=b"v3.17.3\n", stderr=b"")

        chart_path = Path(argv[3])
        values = _yaml_load((workspace / chart_path / "values.yaml").read_text(encoding="utf-8")) or {}
        for index, argument in enumerate(argv[:-1]):
            if argument == "--values":
                overlay_path = workspace / argv[index + 1]
                values = _merge(values, _yaml_load(overlay_path.read_text(encoding="utf-8")) or {})

        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "api", "namespace": "payments"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": "api", "resources": values["app"]["resources"]},
                            {"name": "sidecar", "resources": {"requests": {"cpu": "25m"}}},
                        ]
                    }
                }
            },
        }
        output = f"# Source: charts/payments/templates/deployment.yaml\n---\n{_yaml_dump(deployment)}"
        unrelated = workspace / "charts/payments/templates/configmap.yaml"
        if unrelated.is_file():
            output += """\
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: unrelated
  namespace: payments
data:
  value: changed
"""
        return SandboxResult(returncode=0, stdout=output.encode("utf-8"), stderr=b"")


def _source() -> WorkloadSource:
    return WorkloadSource(
        id="source-helm-values",
        cluster="prod",
        namespace="payments",
        workload_kind="Deployment",
        workload_name="api",
        provider_id=PROVIDER_ID,
        repo_url=REPO_URL,
        ref="main",
        revision=BASE_REVISION,
        path="charts/payments",
        controller_kind=ControllerKind.ARGOCD,
        controller_name="payments",
        controller_namespace="argocd",
        source_revision=BASE_REVISION,
        renderer=RendererKind.HELM_VALUES,
        renderer_config={
            "chart": {"name": None, "repo": REPO_URL},
            "release_name": "payments",
            "release_namespace": "payments",
            "values_files": [VALUES_PATH],
            "values_mapping": MAPPING,
            "values_sources": [],
        },
        confidence=1,
        status=BindingStatus.RESOLVED,
    )


def _action() -> ActionPlanV3:
    return ActionPlanV3(
        action_id="helm-values-action",
        recommendation_ids=[41],
        target=ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="payments", name="api"),
        operations=[
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"cpu": "300m"}),
            )
        ],
        evidence_ref="recommendation/41",
    )


def _create_forward_plan(
    *,
    source_overrides: dict[str, Any] | None = None,
    include_baseline: bool = True,
) -> tuple[Plan, HelmValuesPlanner, dict[str, str], str]:
    source = _source()
    if source_overrides:
        source = source.model_copy(update=source_overrides)
    files = {**CHART_FILES, VALUES_PATH: ORIGINAL_VALUES}
    snapshot = snapshot_from_files(
        files,
        provider_id=PROVIDER_ID,
        repo_url=REPO_URL,
        commit_sha=BASE_REVISION,
        workspace_parent=Path.cwd(),
    )
    planner = HelmValuesPlanner(renderer=HelmRenderer(expected_version="v3.17.3", runner=_HelmRunner()))
    try:
        file_plan = planner.plan(_action(), source, snapshot, BASE_REVISION)
    finally:
        snapshot.close()

    forward_values = file_plan.files[0].content
    assert forward_values is not None
    plan = Plan(
        plan_id=file_plan.plan_id,
        recommendation_ids=[41],
        source=source,
        action=_action(),
        file_plan=file_plan,
        status=PlanStatus.SYNCED,
        digest=file_plan.digest,
        pre_apply_baseline=(
            PreApplyWorkloadBaseline(
                workload_uid="uid-api",
                generation=7,
                observed_generation=7,
                container_requests={
                    "api": ContainerRequestBaseline(cpu_millicores=100, memory_bytes=134217728),
                    "sidecar": ContainerRequestBaseline(cpu_millicores=25, memory_bytes=0),
                },
            )
            if include_baseline
            else None
        ),
    )
    return plan, planner, files, forward_values


def _current_snapshot(files: Mapping[str, str | bytes], revision: str = CURRENT_REVISION) -> RepositorySnapshot:
    return snapshot_from_files(
        files,
        provider_id=PROVIDER_ID,
        repo_url=REPO_URL,
        commit_sha=revision,
        workspace_parent=Path.cwd(),
    )


def test_rollback_source_comparison_allows_only_advanced_argo_values_revisions() -> None:
    original = _source().model_copy(
        update={
            "renderer_config": {
                **_source().renderer_config,
                "values_sources": [{"path": VALUES_PATH, "source_revision": BASE_REVISION}],
            }
        }
    )
    advanced = original.model_copy(
        update={
            "renderer_config": {
                **original.renderer_config,
                "values_sources": [{"path": VALUES_PATH, "source_revision": CURRENT_REVISION}],
            }
        }
    )
    remapped = advanced.model_copy(
        update={
            "renderer_config": {
                **advanced.renderer_config,
                "values_mapping": {
                    **MAPPING,
                    "file_path": "apps/payments/other-values.yaml",
                },
            }
        }
    )

    assert same_rollback_source(original, advanced)
    assert not same_rollback_source(original, remapped)


def test_helm_values_rollback_uses_the_original_mapping_and_observed_baseline() -> None:
    plan, planner, original_files, forward_values = _create_forward_plan()
    current_files = {**original_files, VALUES_PATH: forward_values}
    snapshot = _current_snapshot(current_files)

    try:
        inverse = build_inverse_plan(
            plan,
            {VALUES_PATH: ORIGINAL_VALUES},
            snapshot,
            CURRENT_REVISION,
            renderer=HelmValuesRollbackRenderer(
                planner,
                plan.action,
                workspace_parent=Path.cwd(),
            ),
        )
    finally:
        snapshot.close()

        file_plan = plan.file_plan
        assert file_plan is not None
        assert inverse.renderer is RendererKind.HELM_VALUES
    assert len(inverse.files) == 1
    assert inverse.files[0].path == VALUES_PATH
    assert inverse.files[0].operation == "update"
    assert inverse.files[0].content is not None
    assert "cpu: 100m" in inverse.files[0].content
    assert "cpu: 80m" not in inverse.files[0].content
    assert "memory: 128Mi" in inverse.files[0].content
    assert inverse.rendered_before_digest == file_plan.validation.output_digest
    assert inverse.rendered_before_digest != inverse.rendered_after_digest


def test_helm_values_rollback_reuses_a_plan_only_mapping_against_the_current_binding() -> None:
    plan, planner, original_files, forward_values = _create_forward_plan(
        source_overrides={"evidence": {ONE_TIME_HELM_VALUES_MAPPING_EVIDENCE_KEY: True}},
    )
    current_source = plan.source.model_copy(
        update={
            "renderer_config": {
                key: value for key, value in plan.source.renderer_config.items() if key != "values_mapping"
            },
            "evidence": {},
        }
    )
    snapshot = _current_snapshot({**original_files, VALUES_PATH: forward_values})

    try:
        inverse = build_inverse_plan(
            plan,
            {VALUES_PATH: ORIGINAL_VALUES},
            snapshot,
            CURRENT_REVISION,
            renderer=HelmValuesRollbackRenderer(
                planner,
                plan.action,
                workspace_parent=Path.cwd(),
            ),
            current_source=current_source,
        )
    finally:
        snapshot.close()

    assert len(inverse.files) == 1
    assert inverse.files[0].path == VALUES_PATH
    assert inverse.files[0].content is not None
    assert "cpu: 100m" in inverse.files[0].content


def test_helm_values_rollback_rejects_unrelated_rendered_output_changes() -> None:
    plan, planner, original_files, forward_values = _create_forward_plan()
    current_files = {
        **original_files,
        VALUES_PATH: forward_values,
        "charts/payments/templates/configmap.yaml": "apiVersion: v1\nkind: ConfigMap\n",
    }
    snapshot = _current_snapshot(current_files)

    try:
        with pytest.raises(RollbackConflictError, match="forward plan output"):
            build_inverse_plan(
                plan,
                {VALUES_PATH: ORIGINAL_VALUES},
                snapshot,
                CURRENT_REVISION,
                renderer=HelmValuesRollbackRenderer(
                    planner,
                    plan.action,
                    workspace_parent=Path.cwd(),
                ),
            )
    finally:
        snapshot.close()


def test_helm_values_rollback_rejects_remote_chart_source() -> None:
    plan, planner, original_files, forward_values = _create_forward_plan()
    remote_config = {
        **plan.source.renderer_config,
        "chart": {"name": "payments", "repo": REPO_URL},
    }
    remote_source = plan.source.model_copy(
        update={
            "renderer_config": remote_config,
        }
    )
    plan = plan.model_copy(update={"source": remote_source})
    current_files = {**original_files, VALUES_PATH: forward_values}
    snapshot = _current_snapshot(current_files)

    try:
        with pytest.raises(UnsupportedRollbackError, match="local chart"):
            build_inverse_plan(
                plan,
                {VALUES_PATH: ORIGINAL_VALUES},
                snapshot,
                CURRENT_REVISION,
                renderer=HelmValuesRollbackRenderer(
                    planner,
                    plan.action,
                    workspace_parent=Path.cwd(),
                ),
            )
    finally:
        snapshot.close()


def test_helm_values_rollback_rejects_dependent_charts() -> None:
    plan, planner, original_files, forward_values = _create_forward_plan()
    current_files = {
        **original_files,
        VALUES_PATH: forward_values,
        "charts/payments/Chart.yaml": """\
apiVersion: v2
name: payments
version: 1.0.0
dependencies:
  - name: shared
    version: 1.0.0
    repository: https://charts.example.test
""",
    }
    snapshot = _current_snapshot(current_files)

    try:
        with pytest.raises(UnsupportedRollbackError, match="local chart"):
            build_inverse_plan(
                plan,
                {VALUES_PATH: ORIGINAL_VALUES},
                snapshot,
                CURRENT_REVISION,
                renderer=HelmValuesRollbackRenderer(
                    planner,
                    plan.action,
                    workspace_parent=Path.cwd(),
                ),
            )
    finally:
        snapshot.close()


def test_helm_values_rollback_rejects_missing_explicit_mapping() -> None:
    plan, planner, original_files, forward_values = _create_forward_plan()
    changed_config = {key: value for key, value in plan.source.renderer_config.items() if key != "values_mapping"}
    plan = plan.model_copy(update={"source": plan.source.model_copy(update={"renderer_config": changed_config})})
    snapshot = _current_snapshot({**original_files, VALUES_PATH: forward_values})

    try:
        with pytest.raises(UnsupportedRollbackError, match="mapping path"):
            build_inverse_plan(
                plan,
                {VALUES_PATH: ORIGINAL_VALUES},
                snapshot,
                CURRENT_REVISION,
                renderer=HelmValuesRollbackRenderer(
                    planner,
                    plan.action,
                    workspace_parent=Path.cwd(),
                ),
            )
    finally:
        snapshot.close()


def test_helm_values_rollback_rejects_a_snapshot_from_a_stale_revision() -> None:
    plan, planner, original_files, forward_values = _create_forward_plan()
    snapshot = _current_snapshot({**original_files, VALUES_PATH: forward_values}, revision="c" * 40)

    try:
        with pytest.raises(RollbackConflictError, match="revision"):
            build_inverse_plan(
                plan,
                {VALUES_PATH: ORIGINAL_VALUES},
                snapshot,
                CURRENT_REVISION,
                renderer=HelmValuesRollbackRenderer(
                    planner,
                    plan.action,
                    workspace_parent=Path.cwd(),
                ),
            )
    finally:
        snapshot.close()


def test_helm_values_rollback_rejects_an_argo_values_revision_behind_the_current_ref() -> None:
    renderer_config = {
        **_source().renderer_config,
        "values_sources": [
            {
                "order": 0,
                "source_ref": None,
                "repo_url": REPO_URL,
                "ref": "main",
                "source_path": "charts/payments",
                "path": VALUES_PATH,
                "source_revision": BASE_REVISION,
            }
        ],
    }
    plan, planner, original_files, forward_values = _create_forward_plan(
        source_overrides={"renderer_config": renderer_config}
    )
    current_source = plan.source.model_copy(update={"revision": CURRENT_REVISION, "source_revision": CURRENT_REVISION})
    snapshot = _current_snapshot({**original_files, VALUES_PATH: forward_values})

    try:
        with pytest.raises(UnsupportedRollbackError, match="original unambiguous mapping"):
            build_inverse_plan(
                plan,
                {VALUES_PATH: ORIGINAL_VALUES},
                snapshot,
                CURRENT_REVISION,
                renderer=HelmValuesRollbackRenderer(
                    planner,
                    plan.action,
                    workspace_parent=Path.cwd(),
                ),
                current_source=current_source,
            )
    finally:
        snapshot.close()


@pytest.mark.asyncio
async def test_helm_values_runtime_persists_linked_preview_and_requires_exact_digest_approval() -> None:
    original_config = {
        **_source().renderer_config,
        "values_sources": [
            {
                "order": 0,
                "source_ref": None,
                "repo_url": REPO_URL,
                "ref": "main",
                "source_path": "charts/payments",
                "path": VALUES_PATH,
                "source_revision": BASE_REVISION,
            }
        ],
    }
    original_plan, planner, original_files, forward_values = _create_forward_plan(
        source_overrides={"renderer_config": original_config}
    )
    original_file_plan = original_plan.file_plan
    assert original_file_plan is not None
    current_source_config = {
        **original_plan.source.renderer_config,
        "values_sources": [
            {
                **original_plan.source.renderer_config["values_sources"][0],
                "source_revision": CURRENT_REVISION,
            }
        ],
    }
    current_source = original_plan.source.model_copy(
        update={
            "revision": CURRENT_REVISION,
            "source_revision": CURRENT_REVISION,
            "renderer_config": current_source_config,
        }
    )
    current_files = {**original_files, VALUES_PATH: forward_values}
    recommendation = RecommendationRecord(
        id=41,
        container_name="api",
        namespace="payments",
        owner_kind="Deployment",
        owner_name="api",
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
        async def get_recommendation_by_id(self, rec_id: int) -> RecommendationRecord | None:
            return recommendation if rec_id == 41 else None

    class Bindings:
        async def get_workload_binding(
            self,
            cluster: str,
            namespace: str,
            workload_kind: str,
            workload_name: str,
        ) -> WorkloadBinding:
            assert (cluster, namespace, workload_kind, workload_name) == (
                "prod",
                "payments",
                "Deployment",
                "api",
            )
            return WorkloadBinding(
                cluster=cluster,
                namespace=namespace,
                workload_kind=workload_kind,
                workload_name=workload_name,
                status=BindingStatus.RESOLVED,
                confidence=1,
                source=current_source,
            )

    class SnapshotResolver:
        async def create_snapshot(self, source: WorkloadSource) -> RepositorySnapshot:
            assert source.id == original_plan.source.id
            assert source.renderer_config["values_sources"][0]["source_revision"] == CURRENT_REVISION
            return _current_snapshot(current_files)

    class BlobResolver:
        async def get_blob_file(self, source: WorkloadSource, path: str, revision: str) -> GitBlobFile:
            assert source.id == original_plan.source.id
            assert path == VALUES_PATH
            assert revision == BASE_REVISION
            edit = original_file_plan.files[0]
            assert edit.original_sha is not None
            return GitBlobFile(path=path, content=ORIGINAL_VALUES, blob_sha=edit.original_sha)

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
            assert source.id == original_plan.source.id
            return CURRENT_REVISION

    class Queue:
        def __init__(self) -> None:
            self.keys: list[str] = []

        async def enqueue(self, plan: Plan, *, idempotency_key: str) -> None:
            assert plan.rollback_of_plan_id == original_plan.plan_id
            self.keys.append(idempotency_key)

    plan_repository = PlanRepository()
    queue = Queue()
    plan_service = AutomationPlanService(
        cast(Any, plan_repository),
        git_blob_resolver=BlobResolver(),
        revision_resolver=RevisionResolver(),
        planners={RendererKind.HELM_VALUES: planner},
        operation_enqueuer=queue,
    )
    runtime = RuntimePlanService(
        recommendation_repository=cast(Any, Recommendations()),
        binding_repository=cast(Any, Bindings()),
        cluster="prod",
        snapshot_resolver=cast(Any, SnapshotResolver()),
        plan_service=plan_service,
        snapshot_workspace_parent=Path.cwd(),
    )

    preview = await runtime.prepare_rollback(original_plan.plan_id)

    assert preview.rollback_of_plan_id == original_plan.plan_id
    assert preview.status is PlanStatus.VALIDATED
    assert preview.file_plan is not None
    assert preview.file_plan.base_revision == CURRENT_REVISION
    assert preview.file_plan.files[0].content is not None
    assert "cpu: 100m" in preview.file_plan.files[0].content
    assert queue.keys == []

    approved = await runtime.approve_plan(preview.plan_id, preview.digest or "")

    assert approved.status is PlanStatus.APPROVED
    assert approved.rollback_of_plan_id == original_plan.plan_id
    assert queue.keys == [f"plan:{preview.plan_id}:{preview.digest}"]
    assert recommendation.status is RecommendationStatus.ROLLBACK_REVIEW


@pytest.mark.asyncio
async def test_helm_values_runtime_rejects_missing_persisted_baseline_before_snapshot() -> None:
    original_plan, _, _, _ = _create_forward_plan(include_baseline=False)

    class PlanService:
        async def read_plan(self, plan_id: str) -> Plan | None:
            return original_plan if plan_id == original_plan.plan_id else None

    class SnapshotResolver:
        called = False

        async def create_snapshot(self, source: WorkloadSource) -> RepositorySnapshot:
            del source
            self.called = True
            return cast(RepositorySnapshot, None)

    snapshot_resolver = SnapshotResolver()
    runtime = RuntimePlanService(
        recommendation_repository=cast(Any, object()),
        binding_repository=cast(Any, object()),
        cluster="prod",
        snapshot_resolver=cast(Any, snapshot_resolver),
        plan_service=cast(Any, PlanService()),
    )

    with pytest.raises(AutomationPlanError) as error:
        await runtime.prepare_rollback(original_plan.plan_id)

    assert error.value.code is AutomationErrorCode.ROLLBACK_NOT_READY
    assert not snapshot_resolver.called


@pytest.mark.asyncio
async def test_helm_values_runtime_rejects_a_changed_source_mapping_before_snapshot() -> None:
    original_plan, _, _, _ = _create_forward_plan()
    recommendation = RecommendationRecord(
        id=41,
        container_name="api",
        namespace="payments",
        owner_kind="Deployment",
        owner_name="api",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="Reduce CPU requests.",
        scope="workload",
        status=RecommendationStatus.ROLLBACK_REVIEW,
        action=original_plan.action,
    )
    changed_source = original_plan.source.model_copy(
        update={
            "renderer_config": {
                **original_plan.source.renderer_config,
                "values_mapping": {
                    **MAPPING,
                    "file_path": "apps/payments/other-values.yaml",
                },
            }
        }
    )

    class Recommendations:
        async def get_recommendation_by_id(self, rec_id: int) -> RecommendationRecord | None:
            return recommendation if rec_id == 41 else None

    class Bindings:
        async def get_workload_binding(
            self,
            cluster: str,
            namespace: str,
            workload_kind: str,
            workload_name: str,
        ) -> WorkloadBinding:
            return WorkloadBinding(
                cluster=cluster,
                namespace=namespace,
                workload_kind=workload_kind,
                workload_name=workload_name,
                status=BindingStatus.RESOLVED,
                confidence=1,
                source=changed_source,
            )

    class PlanService:
        def clock(self) -> datetime:
            return datetime.now(timezone.utc)

        async def read_plan(self, plan_id: str) -> Plan | None:
            return original_plan if plan_id == original_plan.plan_id else None

    class SnapshotResolver:
        called = False

        async def create_snapshot(self, source: WorkloadSource) -> RepositorySnapshot:
            del source
            self.called = True
            raise AssertionError("stale source must fail before snapshot creation")

    snapshot_resolver = SnapshotResolver()
    runtime = RuntimePlanService(
        recommendation_repository=cast(Any, Recommendations()),
        binding_repository=cast(Any, Bindings()),
        cluster="prod",
        snapshot_resolver=cast(Any, snapshot_resolver),
        plan_service=cast(Any, PlanService()),
    )

    with pytest.raises(AutomationPlanError) as error:
        await runtime.prepare_rollback(original_plan.plan_id)

    assert error.value.code is AutomationErrorCode.SOURCE_CHANGED
    assert not snapshot_resolver.called
