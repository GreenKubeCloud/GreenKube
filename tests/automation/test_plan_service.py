"""Contract tests for the GitOps automation plan service."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from ruamel.yaml import YAML

from greenkube.automation.git.base import GitFile, GitProvider
from greenkube.automation.plans import (
    AutomationPlanError,
    AutomationPlanService,
    GitBlobFile,
    ProviderGitBlobResolver,
    _archive_contents,
)
from greenkube.gitops.contracts import (
    ActionPlanV3,
    ActionStatus,
    AutomationErrorCode,
    BindingStatus,
    ContainerResources,
    ControllerKind,
    FileEdit,
    FilePlan,
    Plan,
    PlanStatus,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    ValidationResult,
    WorkloadSource,
)
from greenkube.gitops.planning.helm import HelmValuesNeedsMappingError, HelmValuesPlanner
from greenkube.gitops.planning.kustomize import KustomizePlanError
from greenkube.gitops.planning.raw_manifest import (
    RawManifestPlanner,
)
from greenkube.gitops.planning.raw_manifest import (
    RepositorySnapshot as RawManifestSnapshot,
)
from greenkube.gitops.renderers.raw import RawRenderResult, RenderedResource
from greenkube.gitops.snapshots import snapshot_from_files
from greenkube.gitops.snapshots.models import (
    RepositorySnapshot as ArchiveRepositorySnapshot,
)
from greenkube.gitops.snapshots.models import (
    SnapshotFile,
    SnapshotMetadata,
)
from greenkube.storage.base_gitops_repository import AutomationPlanRecord, AutomationPlanRepository

BASE_REVISION = "a" * 40
GIT_BLOB_SHA = "b" * 40
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)

DEPLOYMENT = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: payments
spec:
  template:
    spec:
      containers:
        - name: api
          image: example/api:v1
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
"""


def _source(**overrides: Any) -> WorkloadSource:
    values = {
        "id": "source-1",
        "cluster": "prod",
        "namespace": "payments",
        "workload_kind": "Deployment",
        "workload_name": "api",
        "provider_id": "github-main",
        "repo_url": "https://github.com/acme/gitops.git",
        "ref": "main",
        "revision": BASE_REVISION,
        "path": "apps/payments",
        "controller_kind": ControllerKind.ARGOCD,
        "controller_name": "payments-prod",
        "controller_namespace": "argocd",
        "source_revision": BASE_REVISION,
        "renderer": RendererKind.RAW,
        "renderer_config": {},
        "confidence": 0.98,
        "status": BindingStatus.RESOLVED,
        "evidence": {},
    }
    values.update(overrides)
    return WorkloadSource(**values)


def _action(action_id: str = "plan-1") -> ActionPlanV3:
    return ActionPlanV3(
        action_id=action_id,
        recommendation_ids=[42],
        target=ResourceTarget(
            api_version="apps/v1",
            kind="Deployment",
            namespace="payments",
            name="api",
        ),
        operations=[
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"cpu": "300m", "memory": "256Mi"}),
            )
        ],
        evidence_ref="recommendation/42",
    )


def _archive_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    source: WorkloadSource,
    content: str = DEPLOYMENT,
    commit_sha: str = BASE_REVISION,
) -> ArchiveRepositorySnapshot:
    encoded = content.encode("utf-8")
    relative_path = f"{source.path}/deployment.yaml"
    workspace = Path.cwd()
    file_path = (workspace / relative_path).resolve()
    original_read_bytes = Path.read_bytes

    def read_bytes(path: Path) -> bytes:
        if path.resolve() == file_path:
            return encoded
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    digest = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    return ArchiveRepositorySnapshot(
        metadata=SnapshotMetadata(
            provider_id=source.provider_id,
            repo_url=source.repo_url,
            commit_sha=commit_sha,
            snapshot_id="snapshot-1",
            archive_digest=digest,
            tree_digest=digest,
            compressed_bytes=len(encoded),
            extracted_bytes=len(encoded),
            file_count=1,
        ),
        workspace=workspace,
        files=(SnapshotFile(path=relative_path, size_bytes=len(encoded), digest=digest),),
        _workspace_owner=cast(TemporaryDirectory[str], None),
    )


class MemoryPlanRepository(AutomationPlanRepository):
    def __init__(self) -> None:
        self.plans: dict[str, Plan] = {}
        self.policy_results: dict[str, dict[str, Any]] = {}
        self.action_status: dict[int, ActionStatus] = {}

    async def save_plan(
        self,
        plan: Plan,
        *,
        policy_result: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> Plan:
        self.plans[plan.plan_id] = plan
        self.policy_results[plan.plan_id] = policy_result or {}
        return plan

    async def get_plan(self, plan_id: str) -> Plan | None:
        return self.plans.get(plan_id)

    async def get_plan_record(self, plan_id: str) -> AutomationPlanRecord | None:
        return None

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

    async def list_recommendation_ids_for_plan(self, plan_id: str) -> list[int]:
        plan = self.plans.get(plan_id)
        return list(plan.recommendation_ids) if plan is not None else []

    async def list_plans_for_recommendation(self, recommendation_id: int) -> list[Plan]:
        return [plan for plan in self.plans.values() if recommendation_id in plan.recommendation_ids]

    async def set_recommendation_action_status(
        self,
        recommendation_ids: Sequence[int],
        status: ActionStatus,
        *,
        expected_statuses: Sequence[ActionStatus] | None = None,
        actor: str = "system",
        plan_id: str | None = None,
    ) -> int:
        for recommendation_id in recommendation_ids:
            self.action_status[recommendation_id] = status
        return len(set(recommendation_ids))

    async def get_recommendation_action_status(self, recommendation_id: int) -> ActionStatus | None:
        return self.action_status.get(recommendation_id)

    async def list_recommendations_by_action_status(self, status: ActionStatus) -> list[int]:
        return [recommendation_id for recommendation_id, current in self.action_status.items() if current is status]


class FakeGitBlobResolver:
    def __init__(self, sha: str = GIT_BLOB_SHA) -> None:
        self.sha = sha
        self.lookups: list[tuple[str, str, str]] = []

    async def get_blob_file(self, source: WorkloadSource, path: str, revision: str) -> GitBlobFile | None:
        self.lookups.append((source.provider_id, path, revision))
        return GitBlobFile(path=path, content=DEPLOYMENT, blob_sha=self.sha)


class FakePlanRevisionResolver:
    def __init__(self, revision: str = BASE_REVISION) -> None:
        self.revision = revision
        self.lookups: list[str] = []

    async def resolve_current_revision(self, source: WorkloadSource) -> str:
        self.lookups.append(source.id)
        return self.revision


class InvalidValidationPlanner(RawManifestPlanner):
    def plan(
        self,
        action: ActionPlanV3,
        source: WorkloadSource,
        snapshot: RawManifestSnapshot,
        base_revision: str,
    ) -> FilePlan:
        file_plan = super().plan(action, source, snapshot, base_revision)
        validation = file_plan.validation.model_copy(update={"requests_within_limits": False})
        return file_plan.model_copy(update={"validation": validation})


class FakeKustomizePlanner:
    def __init__(self, error: KustomizePlanError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[ActionPlanV3, WorkloadSource, ArchiveRepositorySnapshot, str]] = []

    def plan(
        self,
        action: ActionPlanV3,
        source: WorkloadSource,
        snapshot: ArchiveRepositorySnapshot,
        base_revision: str,
    ) -> FilePlan:
        self.calls.append((action, source, snapshot, base_revision))
        if self.error is not None:
            raise self.error
        validation = ValidationResult(
            target_changed=True,
            unrelated_changes=False,
            requests_within_limits=True,
            renderer=RendererKind.KUSTOMIZE,
            output_digest=f"sha256:{'c' * 64}",
        )
        edit = FileEdit(
            path=f"{source.path}/kustomization.yaml",
            operation="update",
            original_sha=GIT_BLOB_SHA,
            content="resources: []\n",
        )
        return FilePlan(
            plan_id="pln-kustomize-preview",
            files=[edit],
            diff="--- a/kustomization.yaml\n+++ b/kustomization.yaml\n",
            rendered_diff="--- rendered-before\n+++ rendered-after\n",
            base_revision=base_revision,
            source_revision=source.source_revision,
            renderer=RendererKind.KUSTOMIZE,
            validation=validation,
            digest=f"sha256:{'d' * 64}",
        )


class FakeHelmPlanner:
    def __init__(self, renderer: RendererKind, error: Exception | None = None) -> None:
        self.renderer = renderer
        self.error = error
        self.calls: list[tuple[ActionPlanV3, WorkloadSource, ArchiveRepositorySnapshot, str, dict[str, Any]]] = []

    def plan(
        self,
        action: ActionPlanV3,
        source: WorkloadSource,
        snapshot: ArchiveRepositorySnapshot,
        base_revision: str,
        **kwargs: Any,
    ) -> FilePlan:
        self.calls.append((action, source, snapshot, base_revision, kwargs))
        if self.error is not None:
            raise self.error
        validation = ValidationResult(
            target_changed=True,
            unrelated_changes=False,
            requests_within_limits=True,
            renderer=self.renderer,
            output_digest=f"sha256:{'c' * 64}",
        )
        return FilePlan(
            plan_id=f"pln-{self.renderer.value}",
            files=[
                FileEdit(
                    path=f"{source.path}/values.yaml",
                    operation="update",
                    original_sha=GIT_BLOB_SHA,
                    content="resources:\n  requests:\n    cpu: 300m\n",
                )
            ],
            diff="--- a/values.yaml\n+++ b/values.yaml\n",
            rendered_diff="--- rendered-before\n+++ rendered-after\n",
            base_revision=base_revision,
            source_revision=source.source_revision,
            renderer=self.renderer,
            validation=validation,
            digest=f"sha256:{'d' * 64}",
        )


class _PlanServiceHelmRenderer:
    def render(
        self,
        snapshot: ArchiveRepositorySnapshot,
        chart_path: str,
        *,
        release_name: str,
        release_namespace: str,
        values_files: Sequence[str] = (),
    ) -> RawRenderResult:
        yaml = YAML(typ="safe")
        values_path = snapshot.workspace / chart_path / "values.yaml"
        values = yaml.load(values_path.read_text(encoding="utf-8")) or {}
        if not isinstance(values, dict):
            values = {}
        for path in values_files:
            overlay = yaml.load((snapshot.workspace / path).read_text(encoding="utf-8")) or {}
            if isinstance(overlay, Mapping):
                _merge_plan_service_values(values, overlay)
        app_values = values.get("app")
        resource_values = app_values.get("resources") if isinstance(app_values, Mapping) else None
        resources = dict(resource_values) if isinstance(resource_values, Mapping) else {}
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "api", "namespace": "payments"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{"name": "api", "resources": resources}],
                    }
                }
            },
        }
        digest = hashlib.sha256(repr(manifest).encode("utf-8")).hexdigest()
        return RawRenderResult(
            renderer=RendererKind.HELM_RELEASE,
            resources=(
                RenderedResource(
                    target=ResourceTarget(
                        api_version="apps/v1",
                        kind="Deployment",
                        namespace="payments",
                        name="api",
                    ),
                    manifest=manifest,
                    source_path=f"{chart_path}/templates/deployment.yaml",
                    document_index=0,
                ),
            ),
            warnings=(),
            output_digest=f"sha256:{digest}",
        )


def _merge_plan_service_values(target: dict[str, Any], overlay: Mapping[str, Any]) -> None:
    for key, value in overlay.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            _merge_plan_service_values(current, value)
        else:
            target[key] = value


class IdempotentQueue:
    def __init__(self) -> None:
        self.operations: dict[str, str] = {}
        self.calls = 0

    async def enqueue(self, plan: Plan, *, idempotency_key: str) -> None:
        self.calls += 1
        self.operations.setdefault(idempotency_key, plan.plan_id)


def _service(
    repository: MemoryPlanRepository,
    git_blob_resolver: FakeGitBlobResolver | None = None,
    *,
    revision_resolver: FakePlanRevisionResolver | None = None,
    planners: Mapping[RendererKind, Any] | None = None,
    operation_enqueuer: IdempotentQueue | None = None,
    clock=None,
    plan_ttl: timedelta = timedelta(minutes=30),
) -> AutomationPlanService:
    return AutomationPlanService(
        repository,
        git_blob_resolver=git_blob_resolver or FakeGitBlobResolver(),
        revision_resolver=revision_resolver or FakePlanRevisionResolver(),
        planners=planners,
        operation_enqueuer=operation_enqueuer,
        clock=clock or (lambda: NOW),
        plan_ttl=plan_ttl,
    )


@pytest.mark.asyncio
async def test_create_plan_adapts_archive_snapshot_and_persists_validated_plan(monkeypatch: pytest.MonkeyPatch):
    repository = MemoryPlanRepository()
    resolver = FakeGitBlobResolver()
    source = _source()
    service = _service(repository, resolver)

    plan = await service.create_plan(
        _action(),
        source,
        _archive_snapshot(monkeypatch, source),
        policy_result={"allowed": True},
    )

    assert plan.status is PlanStatus.VALIDATED
    assert plan.plan_id.startswith("pln-")
    assert plan.file_plan is not None
    assert plan.digest == plan.file_plan.digest
    assert plan.file_plan.validation.passed
    assert plan.file_plan.files[0].original_sha == GIT_BLOB_SHA
    assert plan.file_plan.files[0].original_sha != hashlib.sha256(DEPLOYMENT.encode()).hexdigest()
    assert plan.expires_at == NOW + timedelta(minutes=30)
    assert await repository.get_plan(plan.plan_id) == plan
    assert repository.policy_results[plan.plan_id] == {"allowed": True}
    assert repository.action_status[42] is ActionStatus.PLAN_READY
    assert resolver.lookups == [("github-main", "apps/payments/deployment.yaml", BASE_REVISION)]
    from greenkube.api.metrics_endpoint import get_metrics_output

    output = get_metrics_output().decode()
    assert 'greenkube_automation_plans_total{renderer="raw",status="validated"}' in output
    assert 'greenkube_automation_plan_duration_seconds_count{renderer="raw"}' in output


@pytest.mark.asyncio
async def test_preview_plan_does_not_persist_plan_or_action_state(monkeypatch: pytest.MonkeyPatch):
    repository = MemoryPlanRepository()
    source = _source()
    service = _service(repository)

    plan = await service.preview_plan(
        _action(),
        source,
        _archive_snapshot(monkeypatch, source),
    )

    assert plan.status is PlanStatus.VALIDATED
    assert repository.plans == {}
    assert repository.action_status == {}


@pytest.mark.asyncio
async def test_closed_plan_can_be_replanned_at_the_same_revision(monkeypatch: pytest.MonkeyPatch):
    repository = MemoryPlanRepository()
    source = _source()
    action = _action()
    service = _service(repository)
    first = await service.create_plan(action, source, _archive_snapshot(monkeypatch, source))
    await repository.set_plan_status(first.plan_id, PlanStatus.CLOSED)
    repository.action_status[42] = ActionStatus.CLOSED

    second = await service.create_plan(action, source, _archive_snapshot(monkeypatch, source))

    assert second.plan_id == f"{first.plan_id}-attempt-2"
    assert second.digest != first.digest
    assert repository.plans[first.plan_id].status is PlanStatus.CLOSED
    assert repository.plans[second.plan_id] == second
    assert repository.action_status[42] is ActionStatus.PLAN_READY


@pytest.mark.asyncio
async def test_create_plan_dispatches_kustomize_with_verified_archive_snapshot(
    monkeypatch: pytest.MonkeyPatch,
):
    repository = MemoryPlanRepository()
    resolver = FakeGitBlobResolver()
    planner = FakeKustomizePlanner()
    source = _source(
        renderer=RendererKind.KUSTOMIZE,
        renderer_config={"overlay_path": "apps/payments"},
    )
    snapshot = _archive_snapshot(monkeypatch, source)
    service = AutomationPlanService(
        repository,
        git_blob_resolver=resolver,
        planners={RendererKind.KUSTOMIZE: planner},
        clock=lambda: NOW,
    )

    plan = await service.create_plan(_action(), source, snapshot)

    assert plan.status is PlanStatus.VALIDATED
    assert plan.file_plan is not None
    assert plan.file_plan.renderer is RendererKind.KUSTOMIZE
    assert plan.file_plan.validation.passed
    assert planner.calls == [(_action(), source, snapshot, BASE_REVISION)]
    assert planner.calls[0][2] is snapshot
    assert resolver.lookups == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("renderer", "controller_kind"),
    [
        (RendererKind.HELM_VALUES, ControllerKind.ARGOCD),
        (RendererKind.HELM_RELEASE, ControllerKind.FLUX_HELM_RELEASE),
    ],
)
async def test_create_plan_dispatches_supported_helm_renderers(
    monkeypatch: pytest.MonkeyPatch,
    renderer: RendererKind,
    controller_kind: ControllerKind,
):
    repository = MemoryPlanRepository()
    source = _source(
        renderer=renderer,
        controller_kind=controller_kind,
        renderer_config={
            "values_files": ["apps/payments/values.yaml"],
            "values_mapping": {
                "file_path": "apps/payments/values.yaml",
                "container": "api",
                "path": ["container", "resources"],
            },
        },
    )
    planner = FakeHelmPlanner(renderer)
    snapshot = _archive_snapshot(monkeypatch, source)
    service = AutomationPlanService(
        repository,
        planners={renderer: planner},
        clock=lambda: NOW,
    )

    plan = await service.create_plan(_action(), source, snapshot)

    assert plan.status is PlanStatus.VALIDATED
    assert plan.file_plan is not None
    assert plan.file_plan.renderer is renderer
    assert planner.calls == [
        (
            _action(),
            source,
            snapshot,
            BASE_REVISION,
            {"mapping": source.renderer_config["values_mapping"]} if renderer is RendererKind.HELM_VALUES else {},
        )
    ]


@pytest.mark.asyncio
async def test_create_plan_maps_missing_helm_values_mapping_to_chart_mapping_error(
    monkeypatch: pytest.MonkeyPatch,
):
    repository = MemoryPlanRepository()
    source = _source(renderer=RendererKind.HELM_VALUES, renderer_config={"values_files": ["apps/payments/values.yaml"]})
    planner = FakeHelmPlanner(RendererKind.HELM_VALUES, HelmValuesNeedsMappingError("mapping required"))
    service = AutomationPlanService(
        repository,
        planners={RendererKind.HELM_VALUES: planner},
        clock=lambda: NOW,
    )

    with pytest.raises(AutomationPlanError) as error:
        await service.create_plan(_action(), source, _archive_snapshot(monkeypatch, source))

    assert error.value.code is AutomationErrorCode.NEEDS_CHART_MAPPING
    assert repository.plans == {}


@pytest.mark.asyncio
async def test_create_plan_infers_unique_local_helm_values_mapping_without_annotation():
    repository = MemoryPlanRepository()
    source = _source(
        renderer=RendererKind.HELM_VALUES,
        path="charts/payments",
        renderer_config={
            "chart": {"name": None, "repo": "https://github.com/acme/gitops.git"},
            "release_name": "payments",
            "release_namespace": "payments",
            "values_files": ["apps/payments/values.yaml"],
            "values_sources": [],
        },
    )
    snapshot = snapshot_from_files(
        {
            "charts/payments/Chart.yaml": "apiVersion: v2\nname: payments\nversion: 1.0.0\n",
            "charts/payments/values.yaml": "app:\n  resources: {}\n",
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
            "apps/payments/values.yaml": "app:\n  resources:\n    requests:\n      cpu: 100m\n",
        },
        provider_id=source.provider_id,
        repo_url=source.repo_url,
        commit_sha=BASE_REVISION,
        workspace_parent=Path.cwd(),
    )
    service = _service(
        repository,
        planners={
            RendererKind.HELM_VALUES: HelmValuesPlanner(renderer=_PlanServiceHelmRenderer()),
        },
    )

    try:
        plan = await service.create_plan(_action(), source, snapshot)
    finally:
        snapshot.close()

    assert "values_mapping" not in source.renderer_config
    assert plan.status is PlanStatus.VALIDATED
    assert plan.file_plan is not None
    assert plan.file_plan.validation.passed
    assert [edit.path for edit in plan.file_plan.files] == ["apps/payments/values.yaml"]
    assert plan.file_plan.files[0].content is not None
    assert "cpu: 300m" in plan.file_plan.files[0].content
    assert repository.plans[plan.plan_id] == plan


@pytest.mark.asyncio
async def test_create_plan_render_validates_a_one_time_helm_values_mapping():
    repository = MemoryPlanRepository()
    mapping = {
        "file_path": "apps/payments/values.yaml",
        "container": "api",
        "path": ["app", "resources"],
    }
    source = _source(
        renderer=RendererKind.HELM_VALUES,
        path="charts/payments",
        renderer_config={
            "chart": {"name": None, "repo": "https://github.com/acme/gitops.git"},
            "release_name": "payments",
            "release_namespace": "payments",
            "values_files": ["apps/payments/values.yaml"],
            "values_sources": [],
            "values_mapping": mapping,
        },
    )
    snapshot = snapshot_from_files(
        {
            "charts/payments/Chart.yaml": "apiVersion: v2\nname: payments\nversion: 1.0.0\n",
            "charts/payments/values.yaml": "app:\n  resources: {}\n",
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
            "apps/payments/values.yaml": "app:\n  resources:\n    requests:\n      cpu: 100m\n",
        },
        provider_id=source.provider_id,
        repo_url=source.repo_url,
        commit_sha=BASE_REVISION,
        workspace_parent=Path.cwd(),
    )
    service = _service(
        repository,
        planners={
            RendererKind.HELM_VALUES: HelmValuesPlanner(renderer=_PlanServiceHelmRenderer()),
        },
    )

    try:
        plan = await service.create_plan(_action(), source, snapshot)
    finally:
        snapshot.close()

    assert plan.status is PlanStatus.VALIDATED
    assert plan.file_plan is not None
    assert plan.file_plan.validation.passed
    assert [edit.path for edit in plan.file_plan.files] == ["apps/payments/values.yaml"]
    assert plan.file_plan.files[0].content is not None
    assert "cpu: 300m" in plan.file_plan.files[0].content
    assert plan.source.renderer_config["values_mapping"] == mapping


def test_archive_contents_includes_flux_local_chart_files_outside_binding_path():
    manifest_path = "clusters/minikube/flux-helm/helmrelease.yaml"
    source = _source(
        namespace="qa-apps",
        workload_name="qa-flux-helm",
        path="clusters/minikube/flux-helm",
        controller_kind=ControllerKind.FLUX_HELM_RELEASE,
        controller_name="qa-flux-helm",
        controller_namespace="flux-system",
        renderer=RendererKind.HELM_RELEASE,
        renderer_config={
            "chart": {
                "name": "./apps/flux-helm",
                "source_kind": "GitRepository",
                "repo_url": "https://github.com/acme/gitops.git",
            },
            "manifest_source": {"path": manifest_path},
        },
    )
    snapshot = snapshot_from_files(
        {
            manifest_path: "apiVersion: helm.toolkit.fluxcd.io/v2\nkind: HelmRelease\n",
            "clusters/minikube/flux-helm/kustomization.yaml": "resources:\n  - helmrelease.yaml\n",
            "apps/flux-helm/Chart.yaml": "apiVersion: v2\nname: qa-workload\nversion: 0.1.0\n",
            "apps/flux-helm/values.yaml": "web: {}\n",
            "apps/flux-helm/templates/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\n",
            "apps/other/Chart.yaml": "unrelated chart",
        },
        provider_id=source.provider_id,
        repo_url=source.repo_url,
        commit_sha=BASE_REVISION,
        workspace_parent=Path.cwd(),
    )

    try:
        contents, _, revision = _archive_contents(snapshot, source)
    finally:
        snapshot.close()

    assert revision == BASE_REVISION
    assert set(contents) == {
        manifest_path,
        "clusters/minikube/flux-helm/kustomization.yaml",
        "apps/flux-helm/Chart.yaml",
        "apps/flux-helm/values.yaml",
        "apps/flux-helm/templates/deployment.yaml",
    }


@pytest.mark.asyncio
async def test_create_plan_maps_kustomize_planner_errors(monkeypatch: pytest.MonkeyPatch):
    repository = MemoryPlanRepository()
    source = _source(
        renderer=RendererKind.KUSTOMIZE,
        renderer_config={"overlay_path": "apps/payments"},
    )
    planner = FakeKustomizePlanner(KustomizePlanError("unsupported Kustomize components"))
    service = AutomationPlanService(
        repository,
        planners={RendererKind.KUSTOMIZE: planner},
        clock=lambda: NOW,
    )

    with pytest.raises(AutomationPlanError) as error:
        await service.create_plan(_action(), source, _archive_snapshot(monkeypatch, source))

    assert error.value.code.value == "unsupported_source"
    assert "Kustomize planning failed" in str(error.value)
    assert repository.plans == {}


@pytest.mark.asyncio
async def test_create_plan_rejects_ambiguous_sources(monkeypatch: pytest.MonkeyPatch):
    repository = MemoryPlanRepository()
    source = _source(status=BindingStatus.AMBIGUOUS)
    service = _service(repository)

    with pytest.raises(AutomationPlanError) as error:
        await service.create_plan(_action(), source, _archive_snapshot(monkeypatch, source))

    assert error.value.code.value == "ambiguous_binding"
    assert repository.plans == {}


@pytest.mark.asyncio
async def test_create_plan_rejects_invalid_render_without_persisting(monkeypatch: pytest.MonkeyPatch):
    repository = MemoryPlanRepository()
    resolver = FakeGitBlobResolver()
    source = _source()
    service = _service(repository, resolver)

    with pytest.raises(AutomationPlanError) as error:
        await service.create_plan(
            _action(),
            source,
            _archive_snapshot(monkeypatch, source, "apiVersion: [invalid"),
        )

    assert error.value.code.value == "render_validation_failed"
    assert repository.plans == {}
    assert resolver.lookups == []


@pytest.mark.asyncio
async def test_create_plan_rejects_failed_validation_result(monkeypatch: pytest.MonkeyPatch):
    repository = MemoryPlanRepository()
    source = _source()
    service = AutomationPlanService(
        repository,
        git_blob_resolver=FakeGitBlobResolver(),
        planner=InvalidValidationPlanner(),
        clock=lambda: NOW,
    )

    with pytest.raises(AutomationPlanError) as error:
        await service.create_plan(_action(), source, _archive_snapshot(monkeypatch, source))

    assert error.value.code.value == "render_validation_failed"
    assert repository.plans == {}


@pytest.mark.asyncio
async def test_gitlab_provider_resolver_uses_blob_id_not_last_commit_id():
    provider = cast(
        GitProvider,
        SimpleNamespace(
            name="gitlab",
            repository=lambda *args, **kwargs: "repo",
            get_file=AsyncMock(
                return_value=GitFile(
                    path="apps/payments/deployment.yaml",
                    content=DEPLOYMENT,
                    sha="last-commit-sha",
                    blob_sha=GIT_BLOB_SHA,
                )
            ),
        ),
    )
    resolver = ProviderGitBlobResolver({"gitlab-main": provider})

    resolved = await resolver.get_blob_file(
        _source(provider_id="gitlab-main", repo_url="https://gitlab.com/acme/gitops.git"),
        "apps/payments/deployment.yaml",
        BASE_REVISION,
    )

    assert resolved is not None
    assert resolved.blob_sha == GIT_BLOB_SHA


@pytest.mark.asyncio
async def test_gitlab_provider_resolver_fails_closed_without_blob_id():
    provider = cast(
        GitProvider,
        SimpleNamespace(
            name="gitlab",
            repository=lambda *args, **kwargs: "repo",
            get_file=AsyncMock(
                return_value=GitFile(
                    path="apps/payments/deployment.yaml",
                    content=DEPLOYMENT,
                    sha="last-commit-sha",
                )
            ),
        ),
    )
    resolver = ProviderGitBlobResolver({"gitlab-main": provider})

    with pytest.raises(AutomationPlanError) as error:
        await resolver.get_blob_file(
            _source(provider_id="gitlab-main", repo_url="https://gitlab.com/acme/gitops.git"),
            "apps/payments/deployment.yaml",
            BASE_REVISION,
        )

    assert error.value.code.value == "provider_not_ready"
    assert "blob SHA" in str(error.value)


@pytest.mark.asyncio
async def test_create_plan_requires_provider_blob_sha_instead_of_archive_digest(
    monkeypatch: pytest.MonkeyPatch,
):
    repository = MemoryPlanRepository()
    source = _source()
    service = AutomationPlanService(repository, clock=lambda: NOW)

    with pytest.raises(AutomationPlanError) as error:
        await service.create_plan(_action(), source, _archive_snapshot(monkeypatch, source))

    assert error.value.code.value == "provider_not_ready"
    assert "not a Git blob SHA" in str(error.value)
    assert repository.plans == {}


@pytest.mark.asyncio
async def test_create_plan_rejects_planning_snapshot_at_archive_boundary():
    repository = MemoryPlanRepository()
    service = _service(repository)
    source = _source()
    raw_snapshot = RawManifestSnapshot(
        contents={"apps/payments/deployment.yaml": DEPLOYMENT},
        original_blob_shas={"apps/payments/deployment.yaml": GIT_BLOB_SHA},
    )

    with pytest.raises(AutomationPlanError) as error:
        await service.create_plan(_action(), source, cast(ArchiveRepositorySnapshot, raw_snapshot))

    assert error.value.code.value == "unsupported_source"
    assert "gitops.snapshots RepositorySnapshot" in str(error.value)


@pytest.mark.asyncio
async def test_read_plan_returns_persisted_plan_or_none(monkeypatch: pytest.MonkeyPatch):
    repository = MemoryPlanRepository()
    source = _source()
    service = _service(repository)
    plan = await service.create_plan(_action(), source, _archive_snapshot(monkeypatch, source))

    assert await service.read_plan(plan.plan_id) == plan
    assert await service.read_plan("missing") is None


@pytest.mark.asyncio
async def test_approval_requires_exact_digest_and_marks_plan_stale(monkeypatch: pytest.MonkeyPatch):
    repository = MemoryPlanRepository()
    source = _source()
    service = _service(repository)
    plan = await service.create_plan(_action(), source, _archive_snapshot(monkeypatch, source))

    with pytest.raises(AutomationPlanError) as error:
        await service.approve_plan(plan.plan_id, "sha256:" + "f" * 64)

    assert error.value.code.value == "plan_stale"
    stale_plan = await repository.get_plan(plan.plan_id)
    assert stale_plan is not None
    assert stale_plan.status is PlanStatus.STALE
    assert repository.action_status[42] is ActionStatus.NONE


@pytest.mark.asyncio
async def test_approval_expires_plan_and_rejects_it(monkeypatch: pytest.MonkeyPatch):
    repository = MemoryPlanRepository()
    source = _source()
    now = [NOW]
    service = _service(repository, clock=lambda: now[0])
    plan = await service.create_plan(_action(), source, _archive_snapshot(monkeypatch, source))
    now[0] += timedelta(minutes=31)

    with pytest.raises(AutomationPlanError) as error:
        await service.approve_plan(plan.plan_id, plan.digest or "")

    assert error.value.code.value == "plan_expired"
    stale_plan = await repository.get_plan(plan.plan_id)
    assert stale_plan is not None
    assert stale_plan.status is PlanStatus.STALE
    assert repository.action_status[42] is ActionStatus.NONE


@pytest.mark.asyncio
async def test_approval_is_idempotent_and_enqueues_by_plan(monkeypatch: pytest.MonkeyPatch):
    repository = MemoryPlanRepository()
    queue = IdempotentQueue()
    source = _source()
    now = [NOW]
    service = _service(repository, operation_enqueuer=queue, clock=lambda: now[0])
    plan = await service.create_plan(_action(), source, _archive_snapshot(monkeypatch, source))

    digest = plan.digest
    assert digest is not None
    first = await service.approve_plan(plan.plan_id, digest)
    second = await service.approve_plan(plan.plan_id, digest)

    assert first.status is second.status is PlanStatus.APPROVED
    assert len(queue.operations) == 1
    assert queue.operations[f"plan:{plan.plan_id}:{digest}"] == plan.plan_id


@pytest.mark.asyncio
async def test_approval_accepts_unchanged_current_base_revision(monkeypatch: pytest.MonkeyPatch):
    repository = MemoryPlanRepository()
    source = _source()
    revision_resolver = FakePlanRevisionResolver()
    service = _service(repository, revision_resolver=revision_resolver)
    plan = await service.create_plan(_action(), source, _archive_snapshot(monkeypatch, source))
    assert plan.digest is not None

    approved = await service.approve_plan(plan.plan_id, plan.digest)

    assert approved.status is PlanStatus.APPROVED
    assert revision_resolver.lookups == [source.id]


@pytest.mark.asyncio
async def test_approval_marks_plan_stale_when_current_base_revision_moved(
    monkeypatch: pytest.MonkeyPatch,
):
    repository = MemoryPlanRepository()
    source = _source()
    revision_resolver = FakePlanRevisionResolver("c" * 40)
    service = _service(repository, revision_resolver=revision_resolver)
    plan = await service.create_plan(_action(), source, _archive_snapshot(monkeypatch, source))
    assert plan.digest is not None

    with pytest.raises(AutomationPlanError) as error:
        await service.approve_plan(plan.plan_id, plan.digest)

    stale_plan = await repository.get_plan(plan.plan_id)
    assert error.value.code.value == "plan_stale"
    assert stale_plan is not None
    assert stale_plan.status is PlanStatus.STALE
    assert repository.action_status[42] is ActionStatus.NONE
    assert revision_resolver.lookups == [source.id]


@pytest.mark.asyncio
async def test_approval_fails_closed_without_current_revision_resolver(monkeypatch: pytest.MonkeyPatch):
    repository = MemoryPlanRepository()
    source = _source()
    service = AutomationPlanService(
        repository,
        git_blob_resolver=FakeGitBlobResolver(),
        clock=lambda: NOW,
    )
    plan = await service.create_plan(_action(), source, _archive_snapshot(monkeypatch, source))
    assert plan.digest is not None

    with pytest.raises(AutomationPlanError) as error:
        await service.approve_plan(plan.plan_id, plan.digest)

    current = await repository.get_plan(plan.plan_id)
    assert error.value.code.value == "provider_not_ready"
    assert current is not None
    assert current.status is PlanStatus.VALIDATED


@pytest.mark.asyncio
async def test_wrong_digest_does_not_revoke_an_already_approved_plan(monkeypatch: pytest.MonkeyPatch):
    repository = MemoryPlanRepository()
    source = _source()
    service = _service(repository)
    plan = await service.create_plan(_action(), source, _archive_snapshot(monkeypatch, source))
    assert plan.digest is not None
    await service.approve_plan(plan.plan_id, plan.digest)

    with pytest.raises(AutomationPlanError) as error:
        await service.approve_plan(plan.plan_id, "sha256:" + "f" * 64)

    current = await repository.get_plan(plan.plan_id)
    assert error.value.code.value == "plan_stale"
    assert current is not None
    assert current.status is PlanStatus.APPROVED


@pytest.mark.asyncio
async def test_expiration_stales_approved_plan_waiting_to_apply(monkeypatch: pytest.MonkeyPatch):
    repository = MemoryPlanRepository()
    source = _source()
    now = [NOW]
    service = _service(repository, clock=lambda: now[0])
    plan = await service.create_plan(_action(), source, _archive_snapshot(monkeypatch, source))
    assert plan.digest is not None
    approved = await service.approve_plan(plan.plan_id, plan.digest)
    assert approved.status is PlanStatus.APPROVED
    now[0] += timedelta(minutes=31)

    expired = await service.expire_due_plans()

    assert [item.plan_id for item in expired] == [plan.plan_id]
    assert expired[0].status is PlanStatus.STALE
    assert repository.action_status[42] is ActionStatus.NONE


@pytest.mark.asyncio
async def test_creating_replacement_supersedes_previous_plan_and_keeps_action_ready(
    monkeypatch: pytest.MonkeyPatch,
):
    repository = MemoryPlanRepository()
    source = _source()
    service = _service(repository)
    first = await service.create_plan(_action("plan-1"), source, _archive_snapshot(monkeypatch, source))
    refreshed_source = _source(id="source-2")
    second = await service.create_plan(
        _action("plan-2"),
        refreshed_source,
        _archive_snapshot(monkeypatch, refreshed_source),
    )

    superseded_plan = await repository.get_plan(first.plan_id)
    assert superseded_plan is not None
    assert superseded_plan.status is PlanStatus.SUPERSEDED
    assert second.status is PlanStatus.VALIDATED
    assert repository.action_status[42] is ActionStatus.PLAN_READY


@pytest.mark.asyncio
async def test_replanning_same_action_after_source_revision_change_supersedes_old_plan(
    monkeypatch: pytest.MonkeyPatch,
):
    repository = MemoryPlanRepository()
    source = _source()
    service = _service(repository)
    action = _action("same-action")
    first = await service.create_plan(action, source, _archive_snapshot(monkeypatch, source))
    second = await service.create_plan(
        action,
        source,
        _archive_snapshot(monkeypatch, source, commit_sha="b" * 40),
    )

    old_plan = await repository.get_plan(first.plan_id)
    assert old_plan is not None
    assert old_plan.status is PlanStatus.SUPERSEDED
    assert second.plan_id != first.plan_id
    assert second.file_plan is not None
    assert second.file_plan.base_revision == "b" * 40
    assert second.status is PlanStatus.VALIDATED
    assert repository.action_status[42] is ActionStatus.PLAN_READY


@pytest.mark.asyncio
async def test_expire_due_plans_stales_expired_preview_and_clears_action_status(
    monkeypatch: pytest.MonkeyPatch,
):
    repository = MemoryPlanRepository()
    source = _source()
    now = [NOW]
    service = _service(repository, clock=lambda: now[0], plan_ttl=timedelta(minutes=1))
    plan = await service.create_plan(_action(), source, _archive_snapshot(monkeypatch, source))
    now[0] += timedelta(minutes=2)

    expired = await service.expire_due_plans()

    assert [item.plan_id for item in expired] == [plan.plan_id]
    assert expired[0].status is PlanStatus.STALE
    assert repository.action_status[42] is ActionStatus.NONE
