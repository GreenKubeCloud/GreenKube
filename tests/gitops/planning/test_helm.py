from __future__ import annotations

import copy
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from greenkube.gitops.bindings.manual import prepare_manual_binding_resolution
from greenkube.gitops.bindings.resolver import WorkloadBindingResolver
from greenkube.gitops.contracts import (
    ActionPlanV3,
    AutomationErrorCode,
    BindingStatus,
    ContainerResources,
    ControllerKind,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    WorkloadSource,
    binding_candidate_generation_token,
)
from greenkube.gitops.discovery.argo import discover_argo_application
from greenkube.gitops.planning.helm import (
    DEFAULT_HELM_VERSION,
    FluxHelmReleasePlanError,
    FluxHelmReleasePlanner,
    FluxHelmReleaseUnsupportedError,
    HelmValuesMapping,
    HelmValuesNeedsMappingError,
    HelmValuesPlanner,
    HelmValuesUnsupportedError,
    _dump_patch,
    _legacy_target_selector,
    _managed_postrenderer_patches,
    _new_managed_patch,
    _target_selector,
)
from greenkube.gitops.renderers.helm import HelmRenderer
from greenkube.gitops.renderers.sandbox import SandboxResult
from greenkube.gitops.snapshots import RepositorySnapshot, snapshot_from_files

BASE_REVISION = "a" * 40
PROVIDER_ID = "github-main"
REPO_URL = "https://github.com/acme/gitops.git"
HELM_VERSION = "v3.17.3+ge4da497"
TARGET = ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="prod", name="payments-api")
FIXTURES = Path(__file__).parent.parent / "discovery" / "fixtures"

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
    "charts/payments/values-prod.yaml": """\
app:
  resources:
      requests:
        cpu: 250m
        memory: 80Mi
""",
    "charts/payments/templates/deployment.yaml": """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: payments-api
  namespace: prod
spec:
  template:
    spec:
      containers:
        - name: api
          resources:
            {{- toYaml .Values.app.resources | nindent 12 }}
""",
}


def _safe_dump(value: Any) -> str:
    yaml = YAML(typ="safe")
    yaml.default_flow_style = False
    yaml.sort_base_mapping_type_on_output = False
    stream = io.StringIO()
    yaml.dump(value, stream)
    return stream.getvalue()


def _safe_load(content: str) -> Any:
    yaml = YAML(typ="safe")
    yaml.allow_duplicate_keys = False
    return yaml.load(content)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _merge(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(left)
    for key, value in right.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


class _ChartRunner:
    def __init__(
        self,
        *,
        helm_version: str = HELM_VERSION,
        unrelated_on_action: bool = False,
        ignore_values: bool = False,
        ambiguous_mapping: bool = False,
        container_names: Sequence[str] = ("api", "sidecar"),
    ) -> None:
        self.helm_version = helm_version
        self.unrelated_on_action = unrelated_on_action
        self.ignore_values = ignore_values
        self.ambiguous_mapping = ambiguous_mapping
        self.container_names = tuple(container_names)
        self.calls: list[tuple[str, ...]] = []
        self.observed_values: list[dict[str, Any]] = []

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
        invocation = tuple(argv)
        self.calls.append(invocation)
        if invocation[1:] == ("version", "--short"):
            return SandboxResult(returncode=0, stdout=f"{self.helm_version}\n".encode(), stderr=b"")

        chart_path = Path(invocation[3])
        values = _safe_load((workspace / chart_path / "values.yaml").read_text(encoding="utf-8")) or {}
        for index, argument in enumerate(invocation[:-1]):
            if argument == "--values":
                path = workspace / invocation[index + 1]
                overlay = _safe_load(path.read_text(encoding="utf-8")) or {}
                values = _merge(values, overlay)
        self.observed_values.append(copy.deepcopy(values))

        if self.ignore_values:
            resources = {"requests": {"cpu": "100m", "memory": "128Mi"}}
        elif self.ambiguous_mapping:
            app_resources = values["app"]["resources"]
            worker_resources = values["worker"]["resources"]
            resources = app_resources if app_resources["requests"]["cpu"] != "100m" else worker_resources
        else:
            resources = values["app"]["resources"]

        containers: list[dict[str, Any]] = []
        for index, name in enumerate(self.container_names):
            container_resources = resources if index == 0 else {"requests": {"cpu": "25m", "memory": "64Mi"}}
            containers.append({"name": name, "resources": container_resources})
        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "payments-api", "namespace": "prod"},
            "spec": {"template": {"spec": {"containers": containers}}},
        }
        output = f"# Source: {chart_path.as_posix()}/templates/deployment.yaml\n---\n{_safe_dump(deployment)}"
        if self.unrelated_on_action and "cpu: 300m" in output:
            output += """\
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: probe-side-effect
  namespace: prod
data:
  enabled: "true"
"""
        return SandboxResult(returncode=0, stdout=output.encode("utf-8"), stderr=b"")


def _snapshot(files: Mapping[str, str]) -> RepositorySnapshot:
    return snapshot_from_files(
        files,
        provider_id=PROVIDER_ID,
        repo_url=REPO_URL,
        commit_sha=BASE_REVISION,
        workspace_parent=Path.cwd(),
    )


def test_default_helm_pin_matches_the_checksum_pinned_release_build_metadata():
    snapshot = _snapshot(_CHART)
    renderer = HelmRenderer(
        expected_version=DEFAULT_HELM_VERSION,
        runner=_ChartRunner(helm_version="v3.17.3+ge4da497"),
    )

    try:
        metadata = renderer.preflight(snapshot)
    finally:
        snapshot.close()

    assert metadata.version == "v3.17.3+ge4da497"
    assert metadata.is_pinned


def _values_source(
    *,
    values_files: list[str] | None = None,
    renderer_config: dict[str, Any] | None = None,
    **overrides: Any,
) -> WorkloadSource:
    config = renderer_config or {
        "chart": {"name": None, "repo": REPO_URL},
        "release_name": "payments",
        "release_namespace": "prod",
        "values_files": values_files or ["apps/prod/values.yaml"],
        "values_sources": [],
    }
    values = {
        "id": "source-values",
        "cluster": "prod-cluster",
        "namespace": "prod",
        "workload_kind": "Deployment",
        "workload_name": "payments-api",
        "provider_id": PROVIDER_ID,
        "repo_url": REPO_URL,
        "ref": "main",
        "revision": BASE_REVISION,
        "path": "charts/payments",
        "controller_kind": ControllerKind.ARGOCD,
        "controller_name": "payments",
        "controller_namespace": "argocd",
        "source_revision": BASE_REVISION,
        "renderer": RendererKind.HELM_VALUES,
        "renderer_config": config,
        "confidence": 0.99,
        "status": BindingStatus.RESOLVED,
    }
    values.update(overrides)
    return WorkloadSource(**values)


def _flux_source(**overrides: Any) -> WorkloadSource:
    values = {
        "id": "source-flux",
        "cluster": "prod-cluster",
        "namespace": "prod",
        "workload_kind": "Deployment",
        "workload_name": "payments-api",
        "provider_id": PROVIDER_ID,
        "repo_url": REPO_URL,
        "ref": "main",
        "revision": BASE_REVISION,
        "path": "clusters/prod",
        "controller_kind": ControllerKind.FLUX_HELM_RELEASE,
        "controller_name": "payments",
        "controller_namespace": "flux-system",
        "source_revision": f"main@sha1:{BASE_REVISION}",
        "renderer": RendererKind.HELM_RELEASE,
        "renderer_config": {
            "chart": {
                "name": "./charts/payments",
                "version": None,
                "repo_url": REPO_URL,
                "source_kind": "GitRepository",
            },
            "chart_source_ref": {
                "kind": "GitRepository",
                "name": "gitops",
                "namespace": "flux-system",
            },
            "chart_values_files": [],
            "chart_ignore_missing_values_files": False,
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
        "evidence": {
            "source_ref": {
                "kind": "GitRepository",
                "name": "gitops",
                "namespace": "flux-system",
            }
        },
        "confidence": 0.99,
        "status": BindingStatus.RESOLVED,
    }
    values.update(overrides)
    return WorkloadSource(**values)


def _action(
    *,
    requests: dict[str, str] | None = None,
    limits: dict[str, str] | None = None,
    target: ResourceTarget = TARGET,
) -> ActionPlanV3:
    return ActionPlanV3(
        action_id="act-1",
        recommendation_ids=[42],
        target=target,
        operations=[
            SetContainerResources(
                container="api",
                resources=ContainerResources(
                    requests=requests or {"cpu": "300m", "memory": "256Mi"},
                    limits=limits or {},
                ),
            )
        ],
        evidence_ref="recommendation/42",
    )


def _helm_renderer(runner: _ChartRunner) -> HelmRenderer:
    return HelmRenderer(expected_version=HELM_VERSION, runner=runner)


def _values_mapping(path: str = "apps/prod/values.yaml") -> HelmValuesMapping:
    return HelmValuesMapping(file_path=path, container="api", path=("app", "resources"))


def _release_manifest(*, source_kind: str = "GitRepository", values_from: list[dict[str, Any]] | None = None) -> str:
    values_from_section = ""
    if values_from:
        values_from_section = "  valuesFrom:\n" + "".join(
            f"    - kind: {entry['kind']}\n      name: {entry['name']}\n" for entry in values_from
        )
    return f"""\
# Preserve the release's user-owned comment.
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
        kind: {source_kind}
        name: gitops
        namespace: flux-system
  values:
    app:
      replicas: 3
      resources:
        requests:
          cpu: 100m
          memory: 128Mi
{values_from_section}"""


def test_helm_values_planner_uses_explicit_mapping_and_preserves_values_idempotently():
    values_content = """\
# user-owned comment
app:
  replicas: 3
  resources:
    requests:
      cpu: 100m
      memory: 128Mi
    limits:
      cpu: 500m
      memory: 512Mi
featureFlags:
  keep: true
"""
    files = {
        **_CHART,
        "apps/prod/values.yaml": values_content,
    }
    snapshot = _snapshot(files)
    runner = _ChartRunner()
    planner = HelmValuesPlanner(renderer=_helm_renderer(runner))

    try:
        first = planner.plan(_action(), _values_source(), snapshot, BASE_REVISION, mapping=_values_mapping())
        second = planner.plan(_action(), _values_source(), snapshot, BASE_REVISION, mapping=_values_mapping())

        assert first.model_dump(mode="json") == second.model_dump(mode="json")
        assert first.renderer is RendererKind.HELM_VALUES
        assert first.validation.passed
        assert first.validation.target_changed
        assert not first.validation.unrelated_changes
        assert first.validation.requests_within_limits
        assert [edit.path for edit in first.files] == ["apps/prod/values.yaml"]
        assert first.files[0].operation == "update"
        assert first.files[0].original_sha
        planned_values = first.files[0].content
        assert planned_values is not None
        assert "# user-owned comment" in planned_values
        assert "replicas: 3" in planned_values
        assert "featureFlags:\n  keep: true" in planned_values
        assert "cpu: 300m" in planned_values
        assert "memory: 256Mi" in planned_values
        assert "cpu: 500m" in planned_values
        assert len(runner.calls) == 8
        assert (snapshot.workspace / "apps/prod/values.yaml").read_text(encoding="utf-8") == values_content
    finally:
        snapshot.close()


def test_helm_values_planner_can_render_current_source_with_the_explicit_mapping():
    snapshot = _snapshot({**_CHART, "apps/prod/values.yaml": "app:\n  resources:\n    requests:\n      cpu: 300m\n"})
    planner = HelmValuesPlanner(renderer=_helm_renderer(_ChartRunner()))

    try:
        result = planner.render_source(
            _action(),
            _values_source(),
            snapshot,
            BASE_REVISION,
            mapping=_values_mapping(),
        )

        assert result.renderer is RendererKind.HELM_VALUES
        assert any(resource.target == TARGET for resource in result.resources)
    finally:
        snapshot.close()


def test_helm_values_planner_infers_a_unique_values_mapping_with_a_render_probe():
    values_content = """\
app:
  resources:
    requests:
      cpu: 100m
      memory: 128Mi
worker:
  resources:
    requests:
      cpu: 50m
      memory: 64Mi
"""
    snapshot = _snapshot({**_CHART, "apps/prod/values.yaml": values_content})
    runner = _ChartRunner()

    try:
        plan = HelmValuesPlanner(renderer=_helm_renderer(runner)).plan(
            _action(),
            _values_source(),
            snapshot,
            BASE_REVISION,
        )

        assert plan.validation.passed
        assert [item.path for item in plan.files] == ["apps/prod/values.yaml"]
        planned_values = plan.files[0].content
        assert planned_values is not None
        assert "cpu: 300m" in planned_values
        assert runner.calls
    finally:
        snapshot.close()


def test_helm_values_planner_returns_stable_needs_mapping_for_ambiguous_probes():
    values_content = """\
app:
  resources:
    requests:
      cpu: 100m
      memory: 128Mi
worker:
  resources:
    requests:
      cpu: 50m
      memory: 64Mi
    limits:
      cpu: 500m
      memory: 512Mi
"""
    snapshot = _snapshot({**_CHART, "apps/prod/values.yaml": values_content})
    runner = _ChartRunner(ambiguous_mapping=True)

    try:
        with pytest.raises(HelmValuesNeedsMappingError) as error:
            HelmValuesPlanner(renderer=_helm_renderer(runner)).plan(
                _action(),
                _values_source(),
                snapshot,
                BASE_REVISION,
            )

        assert error.value.code is AutomationErrorCode.NEEDS_CHART_MAPPING
        assert error.value.reason_code == "needs_chart_mapping"
        assert str(error.value) == "needs_chart_mapping"
    finally:
        snapshot.close()


def test_helm_values_planner_can_infer_the_chart_default_values_file():
    source = discover_argo_application(
        {
            "metadata": {"name": "payments", "namespace": "argocd"},
            "spec": {
                "destination": {"namespace": "prod"},
                "source": {
                    "repoURL": REPO_URL,
                    "targetRevision": "main",
                    "path": "charts/payments",
                    "helm": {},
                },
            },
            "status": {
                "sync": {"revision": BASE_REVISION},
                "resources": [
                    {
                        "group": "apps",
                        "version": "v1",
                        "kind": "Deployment",
                        "namespace": "prod",
                        "name": "payments-api",
                    }
                ],
            },
        },
        cluster="prod-cluster",
        provider_id=PROVIDER_ID,
    )[0]
    snapshot = _snapshot(_CHART)

    try:
        assert source.status is BindingStatus.RESOLVED
        assert "values_mapping" not in source.renderer_config
        plan = HelmValuesPlanner(renderer=_helm_renderer(_ChartRunner())).plan(
            _action(),
            source,
            snapshot,
            BASE_REVISION,
        )

        assert plan.validation.passed
        assert [item.path for item in plan.files] == ["charts/payments/values.yaml"]
    finally:
        snapshot.close()


def test_helm_values_planner_returns_typed_unsupported_when_mapping_is_ignored():
    values_content = """\
app:
  resources:
    requests:
      cpu: 100m
      memory: 128Mi
"""
    snapshot = _snapshot({**_CHART, "apps/prod/values.yaml": values_content})
    runner = _ChartRunner(ignore_values=True)

    try:
        with pytest.raises(HelmValuesUnsupportedError, match="does not change"):
            HelmValuesPlanner(renderer=_helm_renderer(runner)).plan(
                _action(),
                _values_source(),
                snapshot,
                BASE_REVISION,
                mapping=_values_mapping(),
            )
    finally:
        snapshot.close()


@pytest.mark.parametrize(
    ("mapping", "reason"),
    [
        (_values_mapping(path="apps/prod/other.yaml"), "final Git-managed values file"),
        (HelmValuesMapping("apps/prod/values.yaml", "sidecar", ("app", "resources")), "exact action container"),
        (HelmValuesMapping("apps/prod/values.yaml", "api", ("missing", "resources")), "not a mapping"),
    ],
)
def test_helm_values_planner_rejects_unsupported_explicit_mappings(mapping, reason: str):
    values_content = """\
app:
  resources:
    requests:
      cpu: 100m
      memory: 128Mi
"""
    snapshot = _snapshot({**_CHART, "apps/prod/values.yaml": values_content})

    try:
        with pytest.raises(HelmValuesUnsupportedError, match=reason):
            HelmValuesPlanner(renderer=_helm_renderer(_ChartRunner())).plan(
                _action(),
                _values_source(),
                snapshot,
                BASE_REVISION,
                mapping=mapping,
            )
    finally:
        snapshot.close()


def test_helm_values_planner_accepts_an_explicit_mapping_from_renderer_config():
    values_content = """\
app:
  resources:
    requests:
      cpu: 100m
      memory: 128Mi
"""
    snapshot = _snapshot({**_CHART, "apps/prod/values.yaml": values_content})
    config = {
        **_values_source().renderer_config,
        "values_mapping": {
            "file_path": "apps/prod/values.yaml",
            "container": "api",
            "path": ["app", "resources"],
        },
    }
    source = _values_source(renderer_config=config)

    try:
        plan = HelmValuesPlanner(renderer=_helm_renderer(_ChartRunner())).plan(
            _action(),
            source,
            snapshot,
            BASE_REVISION,
        )
        assert plan.validation.passed
        assert plan.files[0].path == "apps/prod/values.yaml"
    finally:
        snapshot.close()


def test_helm_values_mapping_can_select_a_distinct_same_repository_source_index():
    values_path = "apps/prod/values.yaml"
    config = {
        **_values_source().renderer_config,
        "values_mapping": {
            "file_path": values_path,
            "container": "api",
            "path": ["app", "resources"],
            "source_index": 1,
        },
        "values_sources": [
            {
                "source_index": 1,
                "path": values_path,
                "repo_url": REPO_URL,
                "ref": "main",
                "source_revision": BASE_REVISION,
            }
        ],
    }
    snapshot = _snapshot({**_CHART, values_path: "app:\n  resources:\n    requests:\n      cpu: 100m\n"})

    try:
        plan = HelmValuesPlanner(renderer=_helm_renderer(_ChartRunner())).plan(
            _action(),
            _values_source(renderer_config=config),
            snapshot,
            BASE_REVISION,
        )
    finally:
        snapshot.close()

    assert plan.validation.passed
    assert plan.files[0].path == values_path


def test_helm_values_mapping_fails_closed_for_cross_repository_source_index():
    values_path = "environments/prod/payments.yaml"
    config = {
        **_values_source().renderer_config,
        "values_files": [values_path],
        "values_mapping": {
            "file_path": values_path,
            "container": "api",
            "path": ["app", "resources"],
            "source_index": 1,
        },
        "values_sources": [
            {
                "source_index": 1,
                "path": values_path,
                "repo_url": "https://github.com/acme/environments.git",
                "ref": "release",
                "source_revision": "b" * 40,
            }
        ],
    }
    snapshot = _snapshot(_CHART)

    try:
        with pytest.raises(HelmValuesUnsupportedError, match="cross-repository") as error:
            HelmValuesPlanner(renderer=_helm_renderer(_ChartRunner())).plan(
                _action(),
                _values_source(renderer_config=config),
                snapshot,
                BASE_REVISION,
            )
        assert error.value.code is AutomationErrorCode.UNSUPPORTED_SOURCE
    finally:
        snapshot.close()


def test_manually_selected_argo_helm_source_remains_plannable():
    source = _values_source(evidence={"source_index": 0, "resource": {"kind": "Deployment", "name": "payments-api"}})
    competing = source.model_copy(
        update={
            "id": "source-other",
            "repo_url": "https://github.com/acme/other.git",
            "path": "charts/other",
        }
    )
    binding = WorkloadBindingResolver().resolve([source, competing], now=source.observed_at)
    selected = prepare_manual_binding_resolution(
        binding,
        candidate_id=source.id,
        generation_token=binding_candidate_generation_token(source),
        actor="operator",
        now=source.observed_at,
    ).binding.source
    snapshot = _snapshot({**_CHART, "apps/prod/values.yaml": "app:\n  resources:\n    requests:\n      cpu: 100m\n"})

    assert selected is not None
    assert selected.controller_kind is ControllerKind.ARGOCD
    assert selected.evidence["selected_via"] == "manual"
    try:
        plan = HelmValuesPlanner(renderer=_helm_renderer(_ChartRunner())).plan(
            _action(),
            selected,
            snapshot,
            BASE_REVISION,
        )
    finally:
        snapshot.close()

    assert plan.validation.passed


def test_helm_values_planner_rejects_duplicate_values_file_paths():
    values_path = "apps/prod/values.yaml"
    snapshot = _snapshot({**_CHART, values_path: "app:\n  resources:\n    requests:\n      cpu: 100m\n"})
    source = _values_source(values_files=[values_path, values_path])

    try:
        with pytest.raises(HelmValuesUnsupportedError, match="duplicate paths"):
            HelmValuesPlanner(renderer=_helm_renderer(_ChartRunner())).plan(
                _action(),
                source,
                snapshot,
                BASE_REVISION,
                mapping=_values_mapping(values_path),
            )
    finally:
        snapshot.close()


def test_helm_values_planner_rejects_unknown_mapping_fields():
    values_path = "apps/prod/values.yaml"
    snapshot = _snapshot({**_CHART, values_path: "app:\n  resources:\n    requests:\n      cpu: 100m\n"})
    mapping = {
        "file_path": values_path,
        "container": "api",
        "path": ["app", "resources"],
        "source_index": 0,
        "unexpected": "field",
    }

    try:
        with pytest.raises(HelmValuesUnsupportedError, match="unsupported fields"):
            HelmValuesPlanner(renderer=_helm_renderer(_ChartRunner())).plan(
                _action(),
                _values_source(),
                snapshot,
                BASE_REVISION,
                mapping=mapping,
            )
    finally:
        snapshot.close()


def test_helm_values_planner_fails_closed_for_remote_charts_before_rendering():
    snapshot = _snapshot({**_CHART, "apps/prod/values.yaml": "app: {}\n"})
    runner = _ChartRunner()
    remote_source = _values_source(
        renderer_config={
            "chart": {"name": "payments", "repo": "https://charts.example.test"},
            "release_name": "payments",
            "release_namespace": "prod",
            "values_files": ["apps/prod/values.yaml"],
            "values_sources": [],
        }
    )

    try:
        with pytest.raises(HelmValuesUnsupportedError, match="remote"):
            HelmValuesPlanner(renderer=_helm_renderer(runner)).plan(
                _action(),
                remote_source,
                snapshot,
                BASE_REVISION,
            )
        assert runner.calls == []
    finally:
        snapshot.close()


def test_helm_planners_reject_chart_dependencies_without_resolution():
    chart_files = {
        **_CHART,
        "charts/payments/Chart.yaml": """\
apiVersion: v2
name: payments
version: 1.0.0
dependencies:
  - name: child
    version: 1.0.0
    repository: https://charts.example.test
""",
    }
    values_snapshot = _snapshot({**chart_files, "apps/prod/values.yaml": "app:\n  resources: {}\n"})
    values_runner = _ChartRunner()

    try:
        with pytest.raises(HelmValuesUnsupportedError, match="dependencies"):
            HelmValuesPlanner(renderer=_helm_renderer(values_runner)).plan(
                _action(),
                _values_source(),
                values_snapshot,
                BASE_REVISION,
                mapping=_values_mapping(),
            )
        assert values_runner.calls == []
    finally:
        values_snapshot.close()

    flux_snapshot = _snapshot(
        {
            **chart_files,
            "clusters/prod/helmrelease.yaml": _release_manifest(),
        }
    )
    flux_runner = _ChartRunner()
    try:
        with pytest.raises(FluxHelmReleasePlanError, match="dependencies"):
            FluxHelmReleasePlanner(renderer=_helm_renderer(flux_runner)).plan(
                _action(),
                _flux_source(),
                flux_snapshot,
                BASE_REVISION,
            )
        assert flux_runner.calls == []
    finally:
        flux_snapshot.close()


def test_helm_values_planner_rejects_an_explicit_mapping_that_changes_unrelated_resources():
    values_content = """\
app:
  resources:
    requests:
      cpu: 100m
      memory: 128Mi
"""
    snapshot = _snapshot({**_CHART, "apps/prod/values.yaml": values_content})
    runner = _ChartRunner(unrelated_on_action=True)

    try:
        with pytest.raises(HelmValuesUnsupportedError, match="unrelated"):
            HelmValuesPlanner(renderer=_helm_renderer(runner)).plan(
                _action(),
                _values_source(),
                snapshot,
                BASE_REVISION,
                mapping=_values_mapping(),
            )
    finally:
        snapshot.close()


def test_helm_values_planner_preserves_json_values_files():
    values_content = json.dumps(
        {
            "app": {
                "replicas": 3,
                "resources": {
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                    "limits": {"cpu": "500m", "memory": "512Mi"},
                },
            },
            "featureFlags": {"keep": True},
        },
        indent=2,
    )
    snapshot = _snapshot({**_CHART, "apps/prod/values.json": values_content})
    source = _values_source(values_files=["apps/prod/values.json"])

    try:
        plan = HelmValuesPlanner(renderer=_helm_renderer(_ChartRunner())).plan(
            _action(),
            source,
            snapshot,
            BASE_REVISION,
            mapping=_values_mapping("apps/prod/values.json"),
        )
        assert plan.files[0].path == "apps/prod/values.json"
        assert plan.files[0].content is not None
        updated_values = json.loads(plan.files[0].content)
        assert updated_values["app"]["replicas"] == 3
        assert updated_values["app"]["resources"]["requests"] == {
            "cpu": "300m",
            "memory": "256Mi",
        }
        assert updated_values["app"]["resources"]["limits"]["cpu"] == "500m"
        assert updated_values["featureFlags"]["keep"] is True
    finally:
        snapshot.close()


def test_flux_helmrelease_planner_adds_render_validated_managed_postrenderer_idempotently():
    release_content = _release_manifest()
    files = {
        **_CHART,
        "clusters/prod/helmrelease.yaml": release_content,
    }
    snapshot = _snapshot(files)
    runner = _ChartRunner()
    planner = FluxHelmReleasePlanner(renderer=_helm_renderer(runner))

    try:
        first = planner.plan(_action(), _flux_source(), snapshot, BASE_REVISION)
        second = planner.plan(_action(), _flux_source(), snapshot, BASE_REVISION)

        assert first.model_dump(mode="json") == second.model_dump(mode="json")
        assert first.renderer is RendererKind.HELM_RELEASE
        assert first.validation.passed
        assert first.validation.target_changed
        assert not first.validation.unrelated_changes
        assert len(first.files) == 1
        edit = first.files[0]
        assert edit.path == "clusters/prod/helmrelease.yaml"
        assert edit.operation == "update"
        planned_release = edit.content
        assert planned_release is not None
        assert "# Preserve the release's user-owned comment." in planned_release
        assert "replicas: 3" in planned_release
        release = _safe_load(planned_release)
        post_renderer = release["spec"]["postRenderers"][0]["kustomize"]
        patch_entry = post_renderer["patches"][0]
        assert patch_entry["target"] == {
            "group": "apps",
            "version": "v1",
            "kind": "Deployment",
            "name": r"payments\-api",
            "namespace": "prod",
        }
        patch = _safe_load(patch_entry["patch"])
        assert patch["metadata"]["annotations"]["greenkube.cloud/managed"] == "true"
        assert patch["metadata"]["annotations"]["greenkube.cloud/action-id"] == "act-1"
        assert patch["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"] == {
            "cpu": "300m",
            "memory": "256Mi",
        }
        assert (snapshot.workspace / "clusters/prod/helmrelease.yaml").read_text(encoding="utf-8") == release_content
        assert not list(snapshot.workspace.rglob(".greenkube-render-*"))
    finally:
        snapshot.close()


def test_flux_helmrelease_multi_container_patch_preserves_chart_render_order():
    files = {**_CHART, "clusters/prod/helmrelease.yaml": _release_manifest()}
    snapshot = _snapshot(files)
    runner = _ChartRunner(container_names=("web", "telemetry-sidecar"))
    planner = FluxHelmReleasePlanner(renderer=_helm_renderer(runner))
    action = ActionPlanV3(
        action_id="multi-container",
        recommendation_ids=[42, 43],
        target=TARGET,
        operations=[
            SetContainerResources(
                container="telemetry-sidecar",
                resources=ContainerResources(requests={"cpu": "10m"}),
            ),
            SetContainerResources(
                container="web",
                resources=ContainerResources(requests={"cpu": "300m"}),
            ),
        ],
        constraints={},
        evidence_ref="recommendation/42/43",
    )

    try:
        plan = planner.plan(action, _flux_source(), snapshot, BASE_REVISION)
        assert plan.validation.passed
        release = _safe_load(plan.files[0].content or "")
        patch_entry = release["spec"]["postRenderers"][0]["kustomize"]["patches"][0]
        patch = _safe_load(patch_entry["patch"])
        containers = patch["spec"]["template"]["spec"]["containers"]
        assert [container["name"] for container in containers] == ["web", "telemetry-sidecar"]
    finally:
        snapshot.close()


def test_manually_selected_flux_helmrelease_source_remains_plannable():
    source = _flux_source()
    competing = source.model_copy(
        update={
            "id": "source-flux-other",
            "repo_url": "https://github.com/acme/other.git",
            "path": "clusters/other",
        }
    )
    binding = WorkloadBindingResolver().resolve([source, competing], now=source.observed_at)
    selected = prepare_manual_binding_resolution(
        binding,
        candidate_id=source.id,
        generation_token=binding_candidate_generation_token(source),
        actor="operator",
        now=source.observed_at,
    ).binding.source
    snapshot = _snapshot({**_CHART, "clusters/prod/helmrelease.yaml": _release_manifest()})

    assert selected is not None
    assert selected.controller_kind is ControllerKind.FLUX_HELM_RELEASE
    assert selected.evidence["selected_via"] == "manual"
    assert selected.evidence["source_ref"] == source.evidence["source_ref"]
    try:
        plan = FluxHelmReleasePlanner(renderer=_helm_renderer(_ChartRunner())).plan(
            _action(),
            selected,
            snapshot,
            BASE_REVISION,
        )
    finally:
        snapshot.close()

    assert plan.validation.passed


def test_flux_helmrelease_planner_fails_closed_for_remote_chart_and_runtime_values():
    remote_snapshot = _snapshot(
        {
            **_CHART,
            "clusters/prod/helmrelease.yaml": _release_manifest(source_kind="HelmRepository"),
        }
    )
    remote_runner = _ChartRunner()
    remote_source = _flux_source(
        renderer_config={
            **_flux_source().renderer_config,
            "chart": {
                "name": "payments",
                "version": None,
                "repo_url": "https://charts.example.test",
                "source_kind": "HelmRepository",
            },
        }
    )

    try:
        with pytest.raises(FluxHelmReleasePlanError, match="remote"):
            FluxHelmReleasePlanner(renderer=_helm_renderer(remote_runner)).plan(
                _action(),
                remote_source,
                remote_snapshot,
                BASE_REVISION,
            )
        assert remote_runner.calls == []
    finally:
        remote_snapshot.close()

    values_from_snapshot = _snapshot(
        {
            **_CHART,
            "clusters/prod/helmrelease.yaml": _release_manifest(
                values_from=[{"kind": "ConfigMap", "name": "runtime-values"}]
            ),
        }
    )
    values_runner = _ChartRunner()
    try:
        with pytest.raises(FluxHelmReleasePlanError, match="valuesFrom"):
            FluxHelmReleasePlanner(renderer=_helm_renderer(values_runner)).plan(
                _action(),
                _flux_source(),
                values_from_snapshot,
                BASE_REVISION,
            )
        assert values_runner.calls == []
    finally:
        values_from_snapshot.close()


def test_flux_helmrelease_planner_supports_chart_ref_to_a_local_git_helmchart():
    release = _safe_load(_fixture("flux-helmrelease-chartref.yaml"))
    release["spec"]["chartRef"].pop("kind")
    source_config = {
        **_flux_source().renderer_config,
        "chart": {
            "name": "./charts/payments",
            "version": "1.0.0",
            "repo_url": REPO_URL,
            "source_kind": "GitRepository",
        },
        "chart_source_ref": {
            "kind": "GitRepository",
            "name": "gitops",
            "namespace": "flux-system",
        },
        "chart_values_files": ["charts/payments/values-prod.yaml"],
        "chart_ref": {
            "kind": "HelmChart",
            "name": "payments-chart",
            "namespace": "flux-system",
        },
        "release_name": "prod-payments",
    }
    source = _flux_source(renderer_config=source_config)
    snapshot = _snapshot({**_CHART, "clusters/prod/helmrelease.yaml": _safe_dump(release)})
    runner = _ChartRunner()

    try:
        plan = FluxHelmReleasePlanner(renderer=_helm_renderer(runner)).plan(
            _action(),
            source,
            snapshot,
            BASE_REVISION,
        )
        assert plan.validation.passed
        assert len(plan.files) == 1
        assert plan.files[0].path == "clusters/prod/helmrelease.yaml"
    finally:
        snapshot.close()


def test_flux_helmrelease_planner_uses_chart_spec_values_files_as_replacement_defaults():
    release = _safe_load(_release_manifest())
    release["spec"].pop("values")
    release["spec"]["chart"]["spec"]["valuesFiles"] = [
        "charts/payments/values-prod.yaml",
        "charts/payments/optional.yaml",
    ]
    release["spec"]["chart"]["spec"]["ignoreMissingValuesFiles"] = True
    source_config = {
        **_flux_source().renderer_config,
        "chart_values_files": ["charts/payments/values-prod.yaml", "charts/payments/optional.yaml"],
        "chart_ignore_missing_values_files": True,
    }
    snapshot = _snapshot({**_CHART, "clusters/prod/helmrelease.yaml": _safe_dump(release)})
    runner = _ChartRunner()

    try:
        plan = FluxHelmReleasePlanner(renderer=_helm_renderer(runner)).plan(
            _action(requests={"cpu": "300m"}),
            _flux_source(renderer_config=source_config),
            snapshot,
            BASE_REVISION,
        )
        assert plan.validation.passed
        assert runner.observed_values[0]["app"]["resources"]["requests"] == {
            "cpu": "250m",
            "memory": "80Mi",
        }
        assert len(plan.files) == 1
    finally:
        snapshot.close()


def test_flux_helmrelease_planner_rejects_values_files_missing_from_the_source_snapshot():
    release = _safe_load(_release_manifest())
    release["spec"]["chart"]["spec"]["valuesFiles"] = ["charts/payments/not-in-snapshot.yaml"]
    source_config = {
        **_flux_source().renderer_config,
        "chart_values_files": ["charts/payments/not-in-snapshot.yaml"],
    }
    snapshot = _snapshot({**_CHART, "clusters/prod/helmrelease.yaml": _safe_dump(release)})
    runner = _ChartRunner()

    try:
        with pytest.raises(FluxHelmReleaseUnsupportedError, match="valuesFiles path") as error:
            FluxHelmReleasePlanner(renderer=_helm_renderer(runner)).plan(
                _action(),
                _flux_source(renderer_config=source_config),
                snapshot,
                BASE_REVISION,
            )
        assert error.value.code is AutomationErrorCode.UNSUPPORTED_SOURCE
        assert runner.calls == []
    finally:
        snapshot.close()


def test_flux_helmrelease_planner_uses_git_managed_valuesfrom_in_flux_order():
    release = _safe_load(_fixture("flux-helmrelease-values-from.yaml"))
    release["spec"]["values"] = {"app": {"resources": {"requests": {"cpu": "275m"}}}}
    release_content = _safe_dump(release)
    source_config = {
        **_flux_source().renderer_config,
        "release_name": "prod-payments",
        "values_from": [
            {
                "kind": "ConfigMap",
                "name": "payments-values",
                "namespace": "flux-system",
                "values_key": None,
                "target_path": None,
                "optional": False,
                "git_managed": True,
                "provider_id": PROVIDER_ID,
                "repo_url": REPO_URL,
                "ref": "main",
                "revision": BASE_REVISION,
                "path": "clusters/prod/payments-values.yaml",
            },
            {
                "kind": "Secret",
                "name": "payments-secret-values",
                "namespace": "flux-system",
                "values_key": "values.yaml",
                "target_path": None,
                "optional": False,
                "git_managed": True,
                "provider_id": PROVIDER_ID,
                "repo_url": REPO_URL,
                "ref": "main",
                "revision": BASE_REVISION,
                "path": "clusters/prod/payments-secrets.yaml",
            },
        ],
    }
    snapshot = _snapshot(
        {
            **_CHART,
            "clusters/prod/helmrelease.yaml": release_content,
            "clusters/prod/payments-values.yaml": _fixture("flux-values-configmap.yaml"),
            "clusters/prod/payments-secrets.yaml": _fixture("flux-values-secret.yaml"),
        }
    )
    runner = _ChartRunner()

    try:
        plan = FluxHelmReleasePlanner(renderer=_helm_renderer(runner)).plan(
            _action(),
            _flux_source(renderer_config=source_config),
            snapshot,
            BASE_REVISION,
        )
        assert plan.validation.passed
        assert runner.observed_values[0]["app"]["resources"]["requests"] == {
            "cpu": "275m",
            "memory": "80Mi",
        }
        assert [edit.path for edit in plan.files] == ["clusters/prod/helmrelease.yaml"]
        assert "YXBwOg" not in (plan.files[0].content or "")
    finally:
        snapshot.close()


def test_flux_helmrelease_planner_rejects_valuesfrom_target_path_as_unsupported():
    release = _safe_load(_fixture("flux-helmrelease-values-from.yaml"))
    release["spec"]["valuesFrom"][0]["targetPath"] = "app"
    source_config = {
        **_flux_source().renderer_config,
        "release_name": "prod-payments",
        "values_from": [
            {
                "kind": "ConfigMap",
                "name": "payments-values",
                "namespace": "flux-system",
                "values_key": None,
                "target_path": None,
                "optional": False,
                "git_managed": True,
                "provider_id": PROVIDER_ID,
                "repo_url": REPO_URL,
                "ref": "main",
                "revision": BASE_REVISION,
                "path": "clusters/prod/payments-values.yaml",
            },
            {
                "kind": "Secret",
                "name": "payments-secret-values",
                "namespace": "flux-system",
                "values_key": "values.yaml",
                "target_path": None,
                "optional": False,
                "git_managed": True,
                "provider_id": PROVIDER_ID,
                "repo_url": REPO_URL,
                "ref": "main",
                "revision": BASE_REVISION,
                "path": "clusters/prod/payments-secrets.yaml",
            },
        ],
    }
    snapshot = _snapshot({**_CHART, "clusters/prod/helmrelease.yaml": _safe_dump(release)})
    runner = _ChartRunner()

    try:
        with pytest.raises(FluxHelmReleaseUnsupportedError, match="unsupported-values-from-target-path") as error:
            FluxHelmReleasePlanner(renderer=_helm_renderer(runner)).plan(
                _action(),
                _flux_source(renderer_config=source_config),
                snapshot,
                BASE_REVISION,
            )
        assert error.value.code is AutomationErrorCode.UNSUPPORTED_SOURCE
        assert runner.calls == []
    finally:
        snapshot.close()


def test_flux_helmrelease_planner_rejects_remote_chartref_with_a_typed_reason():
    release = _safe_load(_release_manifest())
    release["spec"].pop("chart")
    release["spec"]["chartRef"] = {
        "kind": "OCIRepository",
        "name": "payments-chart",
        "namespace": "flux-system",
    }
    source_config = {
        **_flux_source().renderer_config,
        "chart_ref": {
            "kind": "OCIRepository",
            "name": "payments-chart",
            "namespace": "flux-system",
        },
    }
    snapshot = _snapshot({**_CHART, "clusters/prod/helmrelease.yaml": _safe_dump(release)})
    runner = _ChartRunner()

    try:
        with pytest.raises(FluxHelmReleaseUnsupportedError, match="remote-chart-not-in-snapshot") as error:
            FluxHelmReleasePlanner(renderer=_helm_renderer(runner)).plan(
                _action(),
                _flux_source(renderer_config=source_config),
                snapshot,
                BASE_REVISION,
            )
        assert error.value.code is AutomationErrorCode.UNSUPPORTED_SOURCE
        assert runner.calls == []
    finally:
        snapshot.close()


def test_flux_helmrelease_planner_accepts_default_wildcard_for_local_git_chart():
    release_content = _release_manifest().replace(
        "      chart: ./charts/payments\n",
        "      chart: ./charts/payments\n      version: '*'\n",
    )
    source_config = dict(_flux_source().renderer_config)
    source_config["chart"] = {**source_config["chart"], "version": "*"}
    source = _flux_source(renderer_config=source_config)
    snapshot = _snapshot({**_CHART, "clusters/prod/helmrelease.yaml": release_content})
    runner = _ChartRunner()

    try:
        plan = FluxHelmReleasePlanner(renderer=_helm_renderer(runner)).plan(
            _action(),
            source,
            snapshot,
            BASE_REVISION,
        )
    finally:
        snapshot.close()

    assert plan.validation.passed
    assert plan.validation.renderer is RendererKind.HELM_RELEASE


def test_flux_helmrelease_planner_uses_flux_default_release_name():
    release_content = _release_manifest().replace("  releaseName: payments\n", "")
    source_config = {**_flux_source().renderer_config, "release_name": "prod-payments"}
    source = _flux_source(renderer_config=source_config)
    snapshot = _snapshot({**_CHART, "clusters/prod/helmrelease.yaml": release_content})

    try:
        result = FluxHelmReleasePlanner(renderer=_helm_renderer(_ChartRunner())).render_source(
            _action(),
            source,
            snapshot,
            BASE_REVISION,
        )
    finally:
        snapshot.close()

    assert any(resource.target == TARGET for resource in result.resources)


def test_flux_helmrelease_planner_does_not_guess_a_shortened_long_release_name():
    controller_name = f"payments-{'x' * 55}"
    release = _safe_load(_release_manifest())
    release["metadata"]["name"] = controller_name
    release["spec"].pop("releaseName")
    expected_name = f"prod-{controller_name}"
    source_config = {**_flux_source().renderer_config, "release_name": expected_name}
    source = _flux_source(controller_name=controller_name, renderer_config=source_config)
    snapshot = _snapshot({**_CHART, "clusters/prod/helmrelease.yaml": _safe_dump(release)})
    runner = _ChartRunner()

    try:
        with pytest.raises(FluxHelmReleaseUnsupportedError, match="53-character limit") as error:
            FluxHelmReleasePlanner(renderer=_helm_renderer(runner)).plan(
                _action(),
                source,
                snapshot,
                BASE_REVISION,
            )
        assert error.value.code is AutomationErrorCode.UNSUPPORTED_SOURCE
        assert runner.calls == []
    finally:
        snapshot.close()


def test_flux_helmrelease_planner_requires_chart_artifact_revision_to_match_snapshot():
    snapshot = _snapshot(
        {
            **_CHART,
            "clusters/prod/helmrelease.yaml": _release_manifest(),
        }
    )
    runner = _ChartRunner()

    try:
        with pytest.raises(FluxHelmReleasePlanError, match="artifact revision"):
            FluxHelmReleasePlanner(renderer=_helm_renderer(runner)).plan(
                _action(),
                _flux_source(source_revision=f"main@sha1:{'b' * 40}"),
                snapshot,
                BASE_REVISION,
            )
        assert runner.calls == []
    finally:
        snapshot.close()


def test_flux_helmrelease_planner_updates_its_managed_patch_without_duplicates():
    original_release = _release_manifest()
    snapshot = _snapshot({**_CHART, "clusters/prod/helmrelease.yaml": original_release})
    runner = _ChartRunner()
    planner = FluxHelmReleasePlanner(renderer=_helm_renderer(runner))

    try:
        first = planner.plan(_action(), _flux_source(), snapshot, BASE_REVISION)
        assert first.files[0].content is not None
        legacy_release = first.files[0].content.replace(r"payments\-api", "payments-api")
        second_snapshot = _snapshot(
            {
                **_CHART,
                "clusters/prod/helmrelease.yaml": legacy_release,
            }
        )
        try:
            updated_action = _action(requests={"cpu": "350m", "memory": "300Mi"}).model_copy(
                update={"action_id": "act-2"}
            )
            second = planner.plan(updated_action, _flux_source(), second_snapshot, BASE_REVISION)
            assert second.files[0].content is not None
            release = _safe_load(second.files[0].content)
            post_renderers = release["spec"]["postRenderers"]
            assert len(post_renderers) == 1
            patch_entry = post_renderers[0]["kustomize"]["patches"][0]
            assert patch_entry["target"]["name"] == r"payments\-api"
            patch = _safe_load(patch_entry["patch"])
            assert patch["metadata"]["annotations"]["greenkube.cloud/action-id"] == "act-2"
            assert patch["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"] == {
                "cpu": "350m",
                "memory": "300Mi",
            }
            assert release["spec"]["values"]["app"]["replicas"] == 3
            assert second.files[0].content is not None
            third_snapshot = _snapshot(
                {
                    **_CHART,
                    "clusters/prod/helmrelease.yaml": second.files[0].content,
                }
            )
            try:
                with pytest.raises(FluxHelmReleasePlanError, match="already present"):
                    planner.plan(updated_action, _flux_source(), third_snapshot, BASE_REVISION)
            finally:
                third_snapshot.close()
        finally:
            second_snapshot.close()
    finally:
        snapshot.close()


def test_flux_postrenderer_selectors_escape_regex_metacharacters_and_read_legacy_selectors():
    target = ResourceTarget(
        api_version="apps/v1",
        kind="Deployment",
        namespace="prod.blue",
        name="payments.api",
    )
    action = _action().model_copy(update={"target": target})
    operations = [operation for operation in action.operations if isinstance(operation, SetContainerResources)]
    patch = _new_managed_patch(action, operations)
    spec = {
        "postRenderers": [
            {
                "kustomize": {
                    "patches": [
                        {
                            "target": _legacy_target_selector(target),
                            "patch": _dump_patch(patch),
                        }
                    ]
                }
            }
        ]
    }

    assert _target_selector(target) == {
        "group": "apps",
        "version": "v1",
        "kind": "Deployment",
        "name": r"payments\.api",
        "namespace": r"prod\.blue",
    }
    assert _managed_postrenderer_patches(spec, FluxHelmReleasePlanError)[0].target == target


def test_flux_helmrelease_planner_refuses_unmanaged_postrenderers():
    release_content = _release_manifest().replace(
        "  values:\n",
        """\
  postRenderers:
    - kustomize:
        patches:
          - target:
              group: apps
              version: v1
              kind: Deployment
              name: payments-api
              namespace: prod
            patch: |-
              apiVersion: apps/v1
              kind: Deployment
              metadata:
                name: payments-api
                namespace: prod
              spec: {}
  values:
""",
    )
    snapshot = _snapshot({**_CHART, "clusters/prod/helmrelease.yaml": release_content})
    runner = _ChartRunner()

    try:
        with pytest.raises(FluxHelmReleasePlanError, match="GreenKube-managed"):
            FluxHelmReleasePlanner(renderer=_helm_renderer(runner)).plan(
                _action(),
                _flux_source(),
                snapshot,
                BASE_REVISION,
            )
        assert (snapshot.workspace / "clusters/prod/helmrelease.yaml").read_text(encoding="utf-8") == release_content
    finally:
        snapshot.close()


def test_flux_helmrelease_planner_preserves_json_release_manifests():
    release_content = json.dumps(_safe_load(_release_manifest()), indent=2) + "\n"
    snapshot = _snapshot({**_CHART, "clusters/prod/helmrelease.json": release_content})
    runner = _ChartRunner()

    try:
        plan = FluxHelmReleasePlanner(renderer=_helm_renderer(runner)).plan(
            _action(),
            _flux_source(),
            snapshot,
            BASE_REVISION,
        )
        assert plan.files[0].path == "clusters/prod/helmrelease.json"
        assert plan.files[0].content is not None
        release = json.loads(plan.files[0].content)
        assert len(release["spec"]["postRenderers"]) == 1
        assert release["spec"]["postRenderers"][0]["kustomize"]["patches"][0]["target"]["namespace"] == "prod"
        assert release["spec"]["values"]["app"]["replicas"] == 3
    finally:
        snapshot.close()


def test_flux_helmrelease_planner_renders_current_and_restored_release_sources():
    original_release = _release_manifest()
    source = _flux_source()
    planner = FluxHelmReleasePlanner(renderer=_helm_renderer(_ChartRunner()))
    original_snapshot = _snapshot({**_CHART, "clusters/prod/helmrelease.yaml": original_release})

    try:
        forward_plan = planner.plan(_action(), source, original_snapshot, BASE_REVISION)
        forward_content = forward_plan.files[0].content
        assert forward_content is not None
        forward_release = _safe_load(forward_content)
        current_source = source.model_copy(
            update={
                "renderer_config": {
                    **source.renderer_config,
                    "post_renderers": forward_release["spec"]["postRenderers"],
                }
            }
        )
        current_snapshot = _snapshot({**_CHART, "clusters/prod/helmrelease.yaml": forward_content})
        restored_snapshot = _snapshot({**_CHART, "clusters/prod/helmrelease.yaml": original_release})

        try:
            current = planner.render_source(_action(), current_source, current_snapshot, BASE_REVISION)
            restored = planner.render_source(_action(), source, restored_snapshot, BASE_REVISION)

            assert current.output_digest == forward_plan.validation.output_digest
            current_target = next(resource for resource in current.resources if resource.target == TARGET)
            restored_target = next(resource for resource in restored.resources if resource.target == TARGET)
            assert current_target.manifest["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"] == {
                "cpu": "300m",
                "memory": "256Mi",
            }
            assert restored_target.manifest["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"] == {
                "cpu": "100m",
                "memory": "128Mi",
            }
        finally:
            current_snapshot.close()
            restored_snapshot.close()
    finally:
        original_snapshot.close()
