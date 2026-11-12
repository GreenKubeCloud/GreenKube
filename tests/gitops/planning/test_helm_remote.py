"""H2: remote (OCI/HTTP) charts, Argo CD multi-source $values apps, mapping registry, several containers."""

from __future__ import annotations

import copy
import hashlib
import io
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from greenkube.gitops.contracts import (
    ActionPlanV3,
    BindingStatus,
    ContainerResources,
    ControllerKind,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    WorkloadSource,
)
from greenkube.gitops.discovery.argo import discover_argo_application
from greenkube.gitops.discovery.flux import discover_flux_helm_release
from greenkube.gitops.planning.helm import (
    FluxHelmReleasePlanner,
    HelmPlanError,
    HelmValuesNeedsMappingError,
    HelmValuesPlanner,
    HelmValuesUnsupportedError,
)
from greenkube.gitops.planning.helm_mappings import (
    ChartIdentity,
    ChartMappingConfigError,
    mapping_candidates,
    parse_chart_mappings,
)
from greenkube.gitops.renderers.charts import ChartReference, FetchedChart, RemoteChartFetcher
from greenkube.gitops.renderers.helm import HelmRenderer
from greenkube.gitops.renderers.sandbox import BoundedSubprocessRunner, SandboxResult
from greenkube.gitops.snapshots import RepositorySnapshot, snapshot_from_files
from tests.gitops.chart_registry import (  # pyrefly: ignore[missing-import]
    FakeChartRegistry,
    chart_files,
    package_chart,
    public_resolver,
)

HELM_VERSION = "v3.22.1+gd6c1a0e"
VALUES_REPO = "https://github.com/acme/env-config.git"
VALUES_REVISION = "c" * 40
VALUES_PATH = "environments/prod/nginx.yaml"
DISCOVERY_FIXTURES = Path(__file__).parent.parent / "discovery" / "fixtures"
TARGET = ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="web", name="web-nginx")
PROD_VALUES = """\
# Production overrides for the public web tier.
replicaCount: 2
metrics:
  enabled: true # scraped by the platform Prometheus
"""

_YAML = YAML(typ="safe")


def _load(content: str) -> Any:
    return _YAML.load(content)


def _dump(value: Any) -> str:
    yaml = YAML(typ="safe")
    yaml.default_flow_style = False
    stream = io.StringIO()
    yaml.dump(value, stream)
    return stream.getvalue()


def _merge(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(left)
    for key, value in right.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


_PRESETS = {
    "nano": {
        "requests": {"cpu": "100m", "memory": "128Mi", "ephemeral-storage": "50Mi"},
        "limits": {"cpu": "150m", "memory": "192Mi", "ephemeral-storage": "2Gi"},
    }
}


class _BitnamiRunner:
    """Emulates ``helm template`` of the recorded Bitnami-style chart (and a plain two-container chart)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

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
            return SandboxResult(returncode=0, stdout=f"{HELM_VERSION}\n".encode(), stderr=b"")
        release, chart_path = invocation[2], Path(invocation[3])
        namespace = invocation[invocation.index("--namespace") + 1]
        chart = _load((workspace / chart_path / "Chart.yaml").read_text(encoding="utf-8"))
        values = _load((workspace / chart_path / "values.yaml").read_text(encoding="utf-8")) or {}
        for index, argument in enumerate(invocation[:-1]):
            if argument == "--values":
                values = _merge(values, _load((workspace / invocation[index + 1]).read_text(encoding="utf-8")) or {})
        if chart["name"] == "nginx":
            containers = [{"name": "nginx", "resources": _bitnami_resources(values)}]
            if values["metrics"]["enabled"]:
                containers.append({"name": "metrics", "resources": _bitnami_resources(values["metrics"])})
            name = f"{release}-nginx"
        else:
            containers = [
                {"name": "api", "resources": values["app"]["resources"]},
                {"name": "sidecar", "resources": values["sidecar"]["resources"]},
            ]
            name = "payments-api"
        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"replicas": values.get("replicaCount", 1), "template": {"spec": {"containers": containers}}},
        }
        output = f"---\n# Source: {chart['name']}/templates/deployment.yaml\n{_dump(deployment)}"
        return SandboxResult(returncode=0, stdout=output.encode(), stderr=b"")


def _bitnami_resources(values: Mapping[str, Any]) -> dict[str, Any]:
    if values.get("resources"):
        return copy.deepcopy(values["resources"])
    return copy.deepcopy(_PRESETS[values["resourcesPreset"]])


class _StubFetcher:
    def __init__(self, chart: FetchedChart) -> None:
        self.chart = chart
        self.calls = 0

    def fetch(self, reference: Any) -> FetchedChart:
        self.calls += 1
        return self.chart


def _application() -> dict[str, Any]:
    with (DISCOVERY_FIXTURES / "argo-multi-source-oci.yaml").open(encoding="utf-8") as stream:
        return _load(stream.read())


def _source(application: Mapping[str, Any] | None = None) -> WorkloadSource:
    sources = discover_argo_application(application or _application(), cluster="prod", provider_id="github-main")
    (source,) = [item for item in sources if item.workload_kind == "Deployment"]
    return source


def _snapshot(files: Mapping[str, str], *, repo_url: str = VALUES_REPO) -> RepositorySnapshot:
    return snapshot_from_files(
        files,
        provider_id="github-main",
        repo_url=repo_url,
        commit_sha=VALUES_REVISION,
        workspace_parent=Path.cwd(),
    )


def _action(operations: Sequence[SetContainerResources], target: ResourceTarget = TARGET) -> ActionPlanV3:
    return ActionPlanV3(
        action_id="act-remote",
        recommendation_ids=[7],
        target=target,
        operations=list(operations),
        evidence_ref="recommendation/7",
    )


def _op(container: str, **requests: str) -> SetContainerResources:
    return SetContainerResources(
        container=container,
        resources=ContainerResources(requests={key.replace("_", "-"): value for key, value in requests.items()}),
    )


def _fetcher(registry: FakeChartRegistry) -> RemoteChartFetcher:
    return RemoteChartFetcher(transport=registry.transport(), resolver=public_resolver)


def _planner(runner: Any, fetcher: Any, **kwargs: Any) -> HelmValuesPlanner:
    return HelmValuesPlanner(
        renderer=HelmRenderer(expected_version=HELM_VERSION, runner=runner),
        chart_fetcher=fetcher,
        chart_mappings=kwargs.pop("chart_mappings", ()),
        **kwargs,
    )


EXPECTED_VALUES = """\
# Production overrides for the public web tier.
replicaCount: 2
metrics:
  enabled: true # scraped by the platform Prometheus
  resources:
    requests:
      cpu: 20m
      ephemeral-storage: 50Mi
      memory: 128Mi
    limits:
      cpu: 150m
      ephemeral-storage: 2Gi
      memory: 192Mi
resources:
  requests:
    cpu: 50m
    ephemeral-storage: 50Mi
    memory: 64Mi
  limits:
    cpu: 150m
    ephemeral-storage: 2Gi
    memory: 192Mi
"""


def test_argo_multi_source_discovery_binds_the_values_repository_and_the_resolved_chart_version():
    source = _source()

    assert source.status is BindingStatus.RESOLVED
    assert source.renderer is RendererKind.HELM_VALUES
    assert source.repo_url == VALUES_REPO
    assert source.revision == VALUES_REVISION
    assert source.renderer_config["values_files"] == [VALUES_PATH]
    assert source.renderer_config["chart"] == {
        "name": "nginx",
        "version": "18.3.*",
        "repo": "registry-1.docker.io/bitnamicharts",
        "resolved_version": "18.3.1",
    }


def test_bitnami_oci_chart_multi_source_plan_edits_the_values_file_for_two_containers():
    registry = FakeChartRegistry()
    runner = _BitnamiRunner()
    snapshot = _snapshot({VALUES_PATH: PROD_VALUES})
    action = _action([_op("nginx", cpu="50m", memory="64Mi"), _op("metrics", cpu="20m")])

    try:
        plan = _planner(runner, _fetcher(registry)).plan(action, _source(), snapshot, VALUES_REVISION)

        assert [edit.path for edit in plan.files] == [VALUES_PATH]
        assert plan.files[0].content == EXPECTED_VALUES
        assert plan.validation.passed
        assert plan.renderer is RendererKind.HELM_VALUES
        assert plan.warnings == [
            "remote Helm chart oci://registry-1.docker.io/bitnamicharts/nginx:18.3.1 rendered from "
            f"{registry.manifest_digest}"
        ]
        templates = [call for call in runner.calls if call[1] == "template"]
        assert templates and all(call[3].startswith(".greenkube-render/charts/") for call in templates)
        assert all(call[3].endswith("/nginx") for call in templates)
        assert not (snapshot.workspace / ".greenkube-render").exists()
        removed = [line for line in plan.rendered_diff.splitlines() if line.startswith("-") and '"cpu"' in line]
        added = [line for line in plan.rendered_diff.splitlines() if line.startswith("+") and '"cpu"' in line]
        assert [line.split(":")[-1].strip() for line in removed] == ['"100m",', '"100m",']
        assert [line.split(":")[-1].strip() for line in added] == ['"50m",', '"20m",']
    finally:
        snapshot.close()


def test_remote_plans_are_deterministic_and_render_twice_for_determinism():
    registry = FakeChartRegistry()
    snapshot = _snapshot({VALUES_PATH: PROD_VALUES})
    action = _action([_op("nginx", cpu="50m")])

    try:
        planner = _planner(_BitnamiRunner(), _fetcher(registry))
        first = planner.plan(action, _source(), snapshot, VALUES_REVISION)
        runner = _BitnamiRunner()
        second = _planner(runner, _fetcher(registry)).plan(action, _source(), snapshot, VALUES_REVISION)

        assert first.digest == second.digest
        templates = [call for call in runner.calls if call[1] == "template"]
        assert len(templates) % 2 == 0
    finally:
        snapshot.close()


def test_configured_chart_mapping_wins_and_a_wrong_one_fails_closed():
    registry = FakeChartRegistry()
    snapshot = _snapshot({VALUES_PATH: PROD_VALUES})
    action = _action([_op("nginx", cpu="50m")])
    wrong = parse_chart_mappings(
        '[{"chart": "nginx", "version": "18.3.1", "container": "nginx", "path": "staticSite.sync.resources"}]'
    )
    right = parse_chart_mappings('[{"chart": "nginx", "version": "18.3.*", "container": "nginx", "path": "resources"}]')

    try:
        with pytest.raises(HelmValuesUnsupportedError, match="configured chart mapping for nginx 18.3.1"):
            _planner(_BitnamiRunner(), _fetcher(registry), chart_mappings=wrong).plan(
                action, _source(), snapshot, VALUES_REVISION
            )
        plan = _planner(_BitnamiRunner(), _fetcher(registry), chart_mappings=right).plan(
            action, _source(), snapshot, VALUES_REVISION
        )
        assert "resources:\n  requests:\n    cpu: 50m" in (plan.files[0].content or "")
    finally:
        snapshot.close()


def _unknown_chart() -> FetchedChart:
    """The recorded chart re-published outside Bitnami: no built-in mapping applies."""
    files = chart_files()
    files["Chart.yaml"] = files["Chart.yaml"].replace(b"bitnamicharts", b"library")
    fetched = _fetcher(FakeChartRegistry(archive=package_chart(files))).fetch(
        ChartReference.parse("registry-1.docker.io/bitnamicharts", "nginx", "18.3.1")
    )
    return FetchedChart(
        reference=ChartReference.parse("oci://registry.example.test/charts", "nginx", "18.3.1"),
        name=fetched.name,
        version=fetched.version,
        digest=fetched.digest,
        archive_digest=fetched.archive_digest,
        files=fetched.files,
    )


def test_unknown_chart_is_mapped_by_a_render_probe_of_the_chart_default_values():
    fetcher = _StubFetcher(_unknown_chart())
    snapshot = _snapshot({VALUES_PATH: PROD_VALUES})

    try:
        plan = _planner(_BitnamiRunner(), fetcher).plan(
            _action([_op("nginx", cpu="50m"), _op("metrics", cpu="20m")]), _source(), snapshot, VALUES_REVISION
        )
        assert plan.files[0].content == EXPECTED_VALUES.replace("    memory: 64Mi\n", "    memory: 128Mi\n")
    finally:
        snapshot.close()


def test_unknown_chart_without_a_resources_path_needs_a_chart_mapping():
    chart = _unknown_chart()
    files = dict(chart.files)
    files["values.yaml"] = files["values.yaml"].replace(b"resources: {}", b"limitsProfile: {}")
    fetcher = _StubFetcher(
        FetchedChart(
            reference=chart.reference,
            name=chart.name,
            version=chart.version,
            digest=chart.digest,
            archive_digest=chart.archive_digest,
            files=files,
        )
    )
    snapshot = _snapshot({VALUES_PATH: PROD_VALUES})

    try:
        with pytest.raises(HelmValuesNeedsMappingError):
            _planner(_BitnamiRunner(), fetcher).plan(
                _action([_op("nginx", cpu="50m")]), _source(), snapshot, VALUES_REVISION
            )
    finally:
        snapshot.close()


def test_chart_fetch_policy_refusals_are_unsupported_before_any_render():
    registry = FakeChartRegistry()
    runner = _BitnamiRunner()
    snapshot = _snapshot({VALUES_PATH: PROD_VALUES})
    fetcher = RemoteChartFetcher(transport=registry.transport(), resolver=public_resolver, allowed_hosts=["ghcr.io"])

    try:
        with pytest.raises(HelmValuesUnsupportedError, match="chart-host-not-allowed"):
            _planner(runner, fetcher).plan(_action([_op("nginx", cpu="50m")]), _source(), snapshot, VALUES_REVISION)
        assert runner.calls == []
    finally:
        snapshot.close()


def test_tampered_chart_fails_render_validation_not_mapping():
    registry = FakeChartRegistry()
    registry.served_archive = package_chart({**chart_files(), "values.yaml": b"replicaCount: 9\n"})
    snapshot = _snapshot({VALUES_PATH: PROD_VALUES})

    try:
        with pytest.raises(HelmPlanError, match="chart-digest-mismatch") as error:
            _planner(_BitnamiRunner(), _fetcher(registry)).plan(
                _action([_op("nginx", cpu="50m")]), _source(), snapshot, VALUES_REVISION
            )
        assert not isinstance(error.value, HelmValuesUnsupportedError)
    finally:
        snapshot.close()


def test_remote_chart_with_inline_values_is_still_rejected():
    discovered = _source()
    source = discovered.model_copy(
        update={"renderer_config": {**discovered.renderer_config, "values_object": {"replicaCount": 3}}}
    )
    snapshot = _snapshot({VALUES_PATH: PROD_VALUES})

    try:
        with pytest.raises(HelmValuesUnsupportedError, match="inline Helm values"):
            _planner(_BitnamiRunner(), _fetcher(FakeChartRegistry())).plan(
                _action([_op("nginx", cpu="50m")]), source, snapshot, VALUES_REVISION
            )
    finally:
        snapshot.close()


def test_explicit_annotation_mapping_for_one_container_and_registry_for_the_other():
    application = _application()
    application["metadata"]["annotations"] = {
        "greenkube.cloud/helm-values-mapping": (
            '{"file_path": "environments/prod/nginx.yaml", "container": "metrics", '
            '"path": ["metrics", "resources"], "source_index": 1}'
        )
    }
    snapshot = _snapshot({VALUES_PATH: PROD_VALUES})

    try:
        plan = _planner(_BitnamiRunner(), _fetcher(FakeChartRegistry())).plan(
            _action([_op("nginx", cpu="50m", memory="64Mi"), _op("metrics", cpu="20m")]),
            _source(application),
            snapshot,
            VALUES_REVISION,
        )
        assert plan.files[0].content == EXPECTED_VALUES
    finally:
        snapshot.close()


_LOCAL_CHART = {
    "charts/payments/Chart.yaml": "apiVersion: v2\nname: payments\nversion: 1.0.0\n",
    "charts/payments/values.yaml": "app:\n  resources: {}\nsidecar:\n  resources: {}\n",
    "charts/payments/templates/deployment.yaml": "# rendered by the test runner\n",
}
_LOCAL_VALUES = """\
app:
  resources:
    requests:
      cpu: 400m
sidecar:
  resources:
    requests:
      cpu: 100m
"""


def _local_source() -> WorkloadSource:
    return WorkloadSource(
        id="source-local",
        cluster="prod",
        namespace="prod",
        workload_kind="Deployment",
        workload_name="payments-api",
        provider_id="github-main",
        repo_url=VALUES_REPO,
        ref="main",
        revision=VALUES_REVISION,
        path="charts/payments",
        controller_kind=ControllerKind.ARGOCD,
        controller_name="payments",
        controller_namespace="argocd",
        source_revision=VALUES_REVISION,
        renderer=RendererKind.HELM_VALUES,
        renderer_config={
            "chart": {"name": None, "repo": VALUES_REPO},
            "release_name": "payments",
            "release_namespace": "prod",
            "values_files": ["apps/prod/values.yaml"],
            "values_sources": [],
        },
        confidence=0.99,
        status=BindingStatus.RESOLVED,
    )


def test_local_chart_plans_several_containers_in_one_values_edit():
    snapshot = _snapshot({**_LOCAL_CHART, "apps/prod/values.yaml": _LOCAL_VALUES})
    target = ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="prod", name="payments-api")

    try:
        plan = _planner(_BitnamiRunner(), _StubFetcher(_unknown_chart())).plan(
            _action([_op("api", cpu="250m"), _op("sidecar", cpu="50m")], target),
            _local_source(),
            snapshot,
            VALUES_REVISION,
        )
        assert plan.files[0].content == _LOCAL_VALUES.replace("400m", "250m").replace("100m", "50m")
        assert plan.warnings == []
    finally:
        snapshot.close()


def test_registry_rules_propose_bitnami_component_paths():
    identity = ChartIdentity(
        name="postgresql",
        version="16.0.0",
        repository="oci://registry-1.docker.io/bitnamicharts",
    )
    candidates, configured = mapping_candidates(identity, "postgresql")

    assert not configured
    assert [candidate.path for candidate in candidates][:3] == [
        ("resources",),
        ("postgresql", "resources"),
        ("primary", "resources"),
    ]
    other, _ = mapping_candidates(ChartIdentity(name="ingress-nginx", version="4.11.0"), "controller")
    assert [candidate.path for candidate in other] == [("controller", "resources")]
    assert mapping_candidates(ChartIdentity(name="in-house", version="1.0.0"), "api") == ((), False)


def test_configured_chart_mappings_are_validated():
    rules = parse_chart_mappings(
        [
            {
                "chart": "oci://registry.example.test/*/api",
                "version": "2.*",
                "container": "api",
                "path": ["a.b", "resources"],
            }
        ]
    )
    identity = ChartIdentity(name="api", version="2.4.0", repository="oci://registry.example.test/team")

    candidates, configured = mapping_candidates(identity, "api", configured=rules)
    assert configured and [candidate.path for candidate in candidates] == [("a.b", "resources")]
    for invalid in ('{"chart": "x"}', '[{"chart": "x", "path": "resources.requests"}]', '[{"path": "resources"}]'):
        with pytest.raises(ChartMappingConfigError):
            parse_chart_mappings(invalid)


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_real_helm_renders_the_recorded_bitnami_chart_from_oci_through_the_planner():
    binary = shutil.which("helm")
    assert binary is not None
    runner = BoundedSubprocessRunner()
    snapshot = _snapshot({VALUES_PATH: PROD_VALUES})
    try:
        version = (
            runner.run(
                [binary, "version", "--short"],
                workspace=snapshot.workspace,
                cwd=snapshot.workspace,
                timeout_seconds=30,
                max_stdout_bytes=1_024,
                max_stderr_bytes=1_024,
            )
            .stdout.decode()
            .strip()
        )
        registry = FakeChartRegistry()
        planner = HelmValuesPlanner(
            renderer=HelmRenderer(expected_version=version, binary=binary, runner=runner),
            chart_fetcher=_fetcher(registry),
            chart_mappings=(),
        )
        plan = planner.plan(
            _action([_op("nginx", cpu="50m", memory="64Mi"), _op("metrics", cpu="20m")]),
            _source(),
            snapshot,
            VALUES_REVISION,
        )

        assert plan.files[0].content == EXPECTED_VALUES
        assert plan.validation.passed
        assert not (snapshot.workspace / ".greenkube-render").exists()
    finally:
        snapshot.close()


PLATFORM_REPO = "https://github.com/acme/platform.git"
RELEASE_PATH = "clusters/prod/web/helmrelease.yaml"
OCI_RELEASE = """\
# Public web tier, chart from the Bitnami OCI registry.
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: web
  namespace: web
spec:
  interval: 10m
  chartRef:
    kind: OCIRepository
    name: nginx-chart
    namespace: flux-system
  values:
    replicaCount: 2
    metrics:
      enabled: true
"""
HTTP_RELEASE = """\
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: web
  namespace: web
spec:
  interval: 10m
  chart:
    spec:
      chart: nginx
      version: 18.3.*
      sourceRef:
        kind: HelmRepository
        name: bitnami
        namespace: flux-system
  values:
    replicaCount: 2
"""


def _flux_live_workload() -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "web-nginx",
            "namespace": "web",
            "labels": {"app.kubernetes.io/managed-by": "Helm"},
            "annotations": {"meta.helm.sh/release-name": "web", "meta.helm.sh/release-namespace": "web"},
        },
    }


def _flux_source(release_content: str, chart_source: Mapping[str, Any]) -> WorkloadSource:
    release = _load(release_content)
    release["status"] = {"history": [{"chartVersion": "18.3.1", "status": "deployed"}]}
    sources = discover_flux_helm_release(
        release,
        [_flux_live_workload()],
        [chart_source],
        cluster="prod",
        provider_id="github-main",
        manifest_source={
            "provider_id": "github-main",
            "repo_url": PLATFORM_REPO,
            "ref": "main",
            "revision": VALUES_REVISION,
            "path": "clusters/prod/web",
        },
    )
    (source,) = sources
    return source


def _oci_repository(registry: FakeChartRegistry, digest: str | None = None) -> dict[str, Any]:
    return {
        "apiVersion": "source.toolkit.fluxcd.io/v1beta2",
        "kind": "OCIRepository",
        "metadata": {"name": "nginx-chart", "namespace": "flux-system"},
        "spec": {"url": "oci://registry-1.docker.io/bitnamicharts/nginx", "ref": {"semver": "18.3.x"}},
        "status": {"artifact": {"revision": f"18.3.1@{digest or registry.manifest_digest}"}},
    }


def test_flux_oci_chart_ref_is_rendered_and_patched_through_the_managed_post_renderer():
    registry = FakeChartRegistry()
    source = _flux_source(OCI_RELEASE, _oci_repository(registry))
    snapshot = _snapshot({RELEASE_PATH: OCI_RELEASE}, repo_url=PLATFORM_REPO)
    runner = _BitnamiRunner()

    try:
        assert source.status is BindingStatus.RESOLVED
        assert source.renderer_config["chart"]["digest"] == registry.manifest_digest
        plan = FluxHelmReleasePlanner(
            renderer=HelmRenderer(expected_version=HELM_VERSION, runner=runner),
            chart_fetcher=_fetcher(registry),
        ).plan(
            _action([_op("nginx", cpu="50m"), _op("metrics", cpu="20m")]),
            source,
            snapshot,
            VALUES_REVISION,
        )

        assert [edit.path for edit in plan.files] == [RELEASE_PATH]
        content = plan.files[0].content or ""
        assert content.startswith("# Public web tier")
        assert "postRenderers:" in content
        assert "- name: nginx" in content and "- name: metrics" in content
        assert plan.validation.passed
        assert plan.warnings == [
            "remote Helm chart oci://registry-1.docker.io/bitnamicharts/nginx:18.3.1 rendered from "
            f"{registry.manifest_digest}"
        ]
        assert all(call[3].startswith(".greenkube-render/charts/") for call in runner.calls if call[1] == "template")
        assert not (snapshot.workspace / ".greenkube-render").exists()
    finally:
        snapshot.close()


def test_flux_oci_chart_whose_tag_moved_since_the_deployment_fails_closed():
    registry = FakeChartRegistry()
    source = _flux_source(OCI_RELEASE, _oci_repository(registry, digest="sha256:" + "f" * 64))
    snapshot = _snapshot({RELEASE_PATH: OCI_RELEASE}, repo_url=PLATFORM_REPO)

    try:
        with pytest.raises(HelmPlanError, match="chart-digest-mismatch"):
            FluxHelmReleasePlanner(
                renderer=HelmRenderer(expected_version=HELM_VERSION, runner=_BitnamiRunner()),
                chart_fetcher=_fetcher(registry),
            ).plan(_action([_op("nginx", cpu="50m")]), source, snapshot, VALUES_REVISION)
    finally:
        snapshot.close()


def test_flux_helm_repository_chart_renders_the_deployed_version_of_a_range():
    registry = FakeChartRegistry()
    helm_repository = {
        "apiVersion": "source.toolkit.fluxcd.io/v1",
        "kind": "HelmRepository",
        "metadata": {"name": "bitnami", "namespace": "flux-system"},
        "spec": {"url": "https://charts.bitnami.com/bitnami"},
        "status": {"artifact": {"revision": "sha256:" + "e" * 64}},
    }
    source = _flux_source(HTTP_RELEASE, helm_repository)
    snapshot = _snapshot({RELEASE_PATH: HTTP_RELEASE}, repo_url=PLATFORM_REPO)

    try:
        assert source.renderer_config["chart"]["resolved_version"] == "18.3.1"
        plan = FluxHelmReleasePlanner(
            renderer=HelmRenderer(expected_version=HELM_VERSION, runner=_BitnamiRunner()),
            chart_fetcher=_fetcher(registry),
        ).plan(_action([_op("nginx", cpu="50m")]), source, snapshot, VALUES_REVISION)

        assert plan.validation.passed
        assert plan.warnings == [
            "remote Helm chart https://charts.bitnami.com/bitnami nginx@18.3.1 rendered from "
            f"sha256:{hashlib.sha256(registry.archive).hexdigest()}"
        ]
    finally:
        snapshot.close()
