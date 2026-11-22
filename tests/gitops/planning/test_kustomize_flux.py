"""Kustomize planning for workloads whose deployed identity differs from their source YAML.

A Flux Kustomization writes ``spec.targetNamespace`` (and ``namePrefix``/``nameSuffix``)
into the root Kustomization before ``kustomize build``; the overlay's own namespace and
name transformers run after its patches. The planner must match the live workload against
the build Flux performs and write a patch selector that Kustomize resolves before those
transformers, while failing closed on anything ambiguous.
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from greenkube.automation.rollback import KustomizeRollbackRenderer
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
from greenkube.gitops.planning.kustomize import KustomizePlanError, KustomizePlanner, render_as_deployed
from greenkube.gitops.renderers.kustomize import KustomizeRenderer
from greenkube.gitops.renderers.sandbox import SandboxResult
from tests.gitops.planning.test_kustomize import BASE_REVISION, _snapshot, _yaml  # pyrefly: ignore[missing-import]

REPO_ROOT = Path(__file__).resolve().parents[3]
OVERLAY = "apps/worker/overlays/prod"
BASE = "apps/worker/base"
TARGET_NAMESPACE = "worker-prod"

WORKER = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: worker
  labels:
    app.kubernetes.io/name: worker
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: worker
  template:
    metadata:
      labels:
        app.kubernetes.io/name: worker
    spec:
      containers:
        - name: worker
          image: busybox:1.36.1
          resources:
            requests:
              cpu: 300m
              memory: 256Mi
            limits:
              cpu: 500m
              memory: 384Mi
"""

WORKER_SERVICE = """\
apiVersion: v1
kind: Service
metadata:
  name: worker
spec:
  selector:
    app.kubernetes.io/name: worker
  ports:
    - port: 8080
"""


def _layout(*, overlay: str = "resources:\n  - ../../base\n", worker: str = WORKER) -> dict[str, str]:
    """A plain Kustomize base and overlay, with namespace-less manifests, deployed by Flux."""
    return {
        f"{BASE}/kustomization.yaml": "resources:\n  - deployment.yaml\n  - service.yaml\n",
        f"{BASE}/deployment.yaml": worker,
        f"{BASE}/service.yaml": WORKER_SERVICE,
        f"{OVERLAY}/kustomization.yaml": overlay,
        # The Flux objects live elsewhere in the repository and are not part of the overlay.
        "clusters/prod/worker.yaml": (
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\nkind: Kustomization\nmetadata:\n"
            "  name: worker\n  namespace: flux-system\nspec:\n  interval: 1m\n  prune: true\n"
            f"  path: ./{OVERLAY}\n  targetNamespace: {TARGET_NAMESPACE}\n"
            "  sourceRef:\n    kind: GitRepository\n    name: fleet\n"
        ),
    }


def _flux_source(
    *,
    namespace: str = TARGET_NAMESPACE,
    name: str = "worker",
    path: str = OVERLAY,
    **renderer_config: Any,
) -> WorkloadSource:
    """The binding Flux discovery produces from the Kustomization inventory."""
    config: dict[str, Any] = {
        "overlay_path": path,
        "kustomization_path": f"{path}/kustomization.yaml",
        "source_ref": {"kind": "GitRepository", "name": "fleet", "namespace": "flux-system"},
        "target_namespace": TARGET_NAMESPACE,
    }
    config.update(renderer_config)
    return WorkloadSource(
        id="flux-worker",
        cluster="prod",
        namespace=namespace,
        workload_kind="Deployment",
        workload_name=name,
        provider_id="github-main",
        repo_url="https://github.com/acme/gitops.git",
        ref="main",
        revision=BASE_REVISION,
        path=path,
        controller_kind=ControllerKind.FLUX_KUSTOMIZATION,
        controller_name="worker",
        controller_namespace="flux-system",
        source_revision=BASE_REVISION,
        renderer=RendererKind.KUSTOMIZE,
        renderer_config={key: value for key, value in config.items() if value is not None},
        confidence=0.95,
        status=BindingStatus.RESOLVED,
        evidence={},
    )


def _action(*, namespace: str = TARGET_NAMESPACE, name: str = "worker", container: str = "worker") -> ActionPlanV3:
    return ActionPlanV3(
        action_id="act-flux",
        recommendation_ids=[7],
        target=ResourceTarget(api_version="apps/v1", kind="Deployment", namespace=namespace, name=name),
        operations=[
            SetContainerResources(
                container=container,
                resources=ContainerResources(requests={"cpu": "50m", "memory": "64Mi"}),
            )
        ],
        constraints={},
        evidence_ref="recommendation/7",
    )


class _KustomizeSemanticsRunner:
    """A small Kustomize model with the build order that matters here.

    Each Kustomization accumulates its resources (bases fully built), applies its patches
    (selectors are anchored regexes; a namespace-less resource matches ``default``), then
    its namespace transformer and its namePrefix/nameSuffix.
    """

    def __init__(self) -> None:
        self.builds: list[dict[str, Any]] = []

    def run(self, argv: Sequence[str], *, workspace: Path, cwd: Path, **_: Any) -> SandboxResult:
        arguments = tuple(argv)
        if arguments[1:] == ("version",):
            return SandboxResult(returncode=0, stdout=b"v5.8.3\n", stderr=b"")
        overlay = workspace / arguments[2]
        self.builds.append(self._config(overlay))
        resources = self._build(overlay)
        stream = io.StringIO()
        YAML(typ="safe").dump_all(resources, stream)
        return SandboxResult(returncode=0, stdout=stream.getvalue().encode(), stderr=b"")

    @staticmethod
    def _config(directory: Path) -> dict[str, Any]:
        for name in ("kustomization.yaml", "kustomization.yml", "Kustomization"):
            candidate = directory / name
            if candidate.is_file():
                return _yaml().load(candidate.read_text(encoding="utf-8")) or {}
        raise AssertionError(f"{directory} has no Kustomization file")

    def _build(self, directory: Path) -> list[dict[str, Any]]:
        config = self._config(directory)
        resources: list[dict[str, Any]] = []
        for reference in config.get("resources", []):
            path = (directory / reference).resolve()
            if path.is_dir():
                resources.extend(self._build(path))
            else:
                resources.extend(doc for doc in _yaml().load_all(path.read_text(encoding="utf-8")) if doc)
        for entry in config.get("patches", []):
            patch = _yaml().load((directory / entry["path"]).read_text(encoding="utf-8"))
            for resource in resources:
                if self._selected(resource, entry["target"]):
                    self._merge_requests(resource, patch)
        for resource in resources:
            metadata = resource.setdefault("metadata", {})
            if config.get("namespace"):
                metadata["namespace"] = config["namespace"]
            metadata["name"] = f"{config.get('namePrefix', '')}{metadata['name']}{config.get('nameSuffix', '')}"
        return resources

    @staticmethod
    def _selected(resource: Mapping[str, Any], selector: Mapping[str, str]) -> bool:
        group, _, version = resource["apiVersion"].rpartition("/")
        metadata = resource["metadata"]
        observed = {
            "group": group,
            "version": version,
            "kind": resource["kind"],
            "name": metadata["name"],
            "namespace": metadata.get("namespace") or "default",
        }
        return all(re.fullmatch(pattern, observed[field]) for field, pattern in selector.items() if pattern)

    @staticmethod
    def _merge_requests(resource: dict[str, Any], patch: Mapping[str, Any]) -> None:
        containers = resource["spec"]["template"]["spec"]["containers"]
        for patch_container in patch["spec"]["template"]["spec"]["containers"]:
            container = next(item for item in containers if item["name"] == patch_container["name"])
            container.setdefault("resources", {}).setdefault("requests", {}).update(
                patch_container["resources"]["requests"]
            )


class _KubectlKustomizeRunner:
    """Real Kustomize through ``kubectl kustomize`` (local only, never contacts a cluster)."""

    def run(self, argv: Sequence[str], *, workspace: Path, cwd: Path, **_: Any) -> SandboxResult:
        arguments = tuple(argv)
        if arguments[1:] == ("version",):
            return SandboxResult(returncode=0, stdout=b"v5.8.3\n", stderr=b"")
        completed = subprocess.run(
            ["kubectl", "kustomize", str(cwd / arguments[2]), *arguments[3:]],
            capture_output=True,
            check=False,
            timeout=60,
        )
        return SandboxResult(returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)


def _kubectl_kustomize_available() -> bool:
    if shutil.which("kubectl") is None:
        return False
    try:
        completed = subprocess.run(["kubectl", "kustomize", "--help"], capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


_RUNNERS = [
    pytest.param(_KustomizeSemanticsRunner, id="model"),
    pytest.param(
        _KubectlKustomizeRunner,
        id="kubectl-kustomize",
        marks=pytest.mark.skipif(not _kubectl_kustomize_available(), reason="kubectl kustomize is not installed"),
    ),
]


def _plan(tmp_path: Path, files: Mapping[str, str], source: WorkloadSource, action: ActionPlanV3, runner: Any):
    snapshot = _snapshot(tmp_path, files)
    try:
        return KustomizePlanner(runner=runner).plan(action, source, snapshot, BASE_REVISION)
    finally:
        snapshot.close()


def _edits(plan: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    kustomization = next(edit for edit in plan.files if edit.path.endswith("kustomization.yaml"))
    patch = next(edit for edit in plan.files if "greenkube-patches/" in edit.path)
    return _yaml().load(kustomization.content or ""), _yaml().load(patch.content or "")


@pytest.mark.parametrize("runner_type", _RUNNERS)
def test_flux_target_namespace_on_a_plain_overlay_is_planned(tmp_path: Path, runner_type: type):
    """Regression (e2e): targetNamespace comes from Flux, not from the overlay or manifests."""
    plan = _plan(tmp_path, _layout(), _flux_source(), _action(), runner_type())

    assert plan.validation.passed
    kustomization, patch = _edits(plan)
    assert "namespace" not in kustomization, "Flux's targetNamespace must never be written to Git"
    # Kustomize resolves the overlay's patches before any namespace is set.
    assert kustomization["patches"][0]["target"] == {
        "group": "apps",
        "version": "v1",
        "kind": "Deployment",
        "name": "worker",
        "namespace": "default",
    }
    assert patch["metadata"] == {"name": "worker", "namespace": TARGET_NAMESPACE}
    assert '"cpu": "300m"' in plan.rendered_diff
    assert '"cpu": "50m"' in plan.rendered_diff


@pytest.mark.parametrize("runner_type", _RUNNERS)
def test_flux_target_namespace_overrides_a_different_overlay_namespace(tmp_path: Path, runner_type: type):
    files = _layout(overlay="namespace: staging\nresources:\n  - ../../base\n")
    files[f"{BASE}/deployment.yaml"] = WORKER.replace("  name: worker\n", "  name: worker\n  namespace: legacy\n", 1)

    plan = _plan(tmp_path, files, _flux_source(), _action(), runner_type())

    assert plan.validation.passed
    kustomization, _ = _edits(plan)
    assert kustomization["namespace"] == "staging"
    assert kustomization["patches"][0]["target"]["namespace"] == "legacy"


@pytest.mark.parametrize("runner_type", _RUNNERS)
def test_overlay_name_prefix_and_suffix_are_resolved_before_patching(tmp_path: Path, runner_type: type):
    files = _layout(overlay="namePrefix: prod-\nnameSuffix: -v2\nresources:\n  - ../../base\n")

    plan = _plan(tmp_path, files, _flux_source(name="prod-worker-v2"), _action(name="prod-worker-v2"), runner_type())

    assert plan.validation.passed
    kustomization, patch = _edits(plan)
    assert kustomization["patches"][0]["target"]["name"] == "worker"
    assert kustomization["patches"][0]["target"]["namespace"] == "default"
    assert patch["metadata"] == {"name": "prod-worker-v2", "namespace": TARGET_NAMESPACE}


@pytest.mark.parametrize("runner_type", _RUNNERS)
def test_flux_name_prefix_is_applied_like_the_controller(tmp_path: Path, runner_type: type):
    plan = _plan(
        tmp_path,
        _layout(),
        _flux_source(name="eu-worker", name_prefix="eu-"),
        _action(name="eu-worker"),
        runner_type(),
    )

    assert plan.validation.passed
    kustomization, _ = _edits(plan)
    assert "namePrefix" not in kustomization
    assert kustomization["patches"][0]["target"]["name"] == "worker"


@pytest.mark.parametrize("runner_type", _RUNNERS)
def test_argocd_destination_namespace_matches_a_namespace_less_overlay(tmp_path: Path, runner_type: type):
    source = _flux_source(target_namespace=None).model_copy(
        update={"controller_kind": ControllerKind.ARGOCD, "controller_namespace": "argocd"}
    )

    plan = _plan(tmp_path, _layout(), source, _action(), runner_type())

    assert plan.validation.passed
    kustomization, _ = _edits(plan)
    assert kustomization["patches"][0]["target"]["namespace"] == "default"


def test_quickstart_flux_overlay_is_planned(tmp_path: Path):
    """The documented quickstart: a plain overlay deployed with targetNamespace."""
    sample = REPO_ROOT / "docs/examples/quickstart/gitops-repo/flux/oversized-worker"
    files = {f"flux/oversized-worker/{path.name}": path.read_text(encoding="utf-8") for path in sample.iterdir()}
    source = _flux_source(
        namespace="greenkube-e2e-apps",
        name="oversized-worker",
        path="flux/oversized-worker",
        target_namespace="greenkube-e2e-apps",
    )
    action = _action(namespace="greenkube-e2e-apps", name="oversized-worker")

    plan = _plan(tmp_path, files, source, action, _KustomizeSemanticsRunner())

    assert plan.validation.passed
    assert sorted(edit.path.rsplit("/", 1)[0] for edit in plan.files) == [
        "flux/oversized-worker",
        "flux/oversized-worker/greenkube-patches",
    ]


@pytest.mark.parametrize("runner_type", _RUNNERS)
def test_namespace_still_identifies_the_workload_without_a_namespace_transformer(tmp_path: Path, runner_type: type):
    """Two same-named workloads in different namespaces are not ambiguous when only names change."""
    files = _layout(overlay="namePrefix: prod-\nresources:\n  - ../../base\n")
    files[f"{BASE}/deployment.yaml"] = "---\n".join(
        WORKER.replace("  name: worker\n", f"  name: worker\n  namespace: {namespace}\n", 1)
        for namespace in ("team-a", "team-b")
    )
    files[f"{BASE}/service.yaml"] = WORKER_SERVICE.replace("  name: worker\n", "  name: worker\n  namespace: team-a\n")
    source = _flux_source(namespace="team-b", name="prod-worker", target_namespace=None).model_copy(
        update={"controller_kind": ControllerKind.ARGOCD, "controller_namespace": "argocd"}
    )

    plan = _plan(tmp_path, files, source, _action(namespace="team-b", name="prod-worker"), runner_type())

    assert plan.validation.passed
    kustomization, _ = _edits(plan)
    assert kustomization["patches"][0]["target"]["namespace"] == re.escape("team-b")
    assert kustomization["patches"][0]["target"]["name"] == "worker"


def test_the_flux_build_overrides_never_reach_the_repository(tmp_path: Path):
    files = _layout()
    snapshot = _snapshot(tmp_path, files)
    runner = _KustomizeSemanticsRunner()
    try:
        plan = KustomizePlanner(runner=runner).plan(
            _action(name="eu-worker"),
            _flux_source(name="eu-worker", name_prefix="eu-"),
            snapshot,
            BASE_REVISION,
        )
        assert (snapshot.workspace / OVERLAY / "kustomization.yaml").read_text(encoding="utf-8") == files[
            f"{OVERLAY}/kustomization.yaml"
        ]
    finally:
        snapshot.close()

    assert plan.validation.passed
    # before (as Flux builds it), probe (before the identity transformers), after (as Flux builds it)
    assert [(build.get("namespace"), build.get("namePrefix")) for build in runner.builds] == [
        (TARGET_NAMESPACE, "eu-"),
        (None, None),
        (TARGET_NAMESPACE, "eu-"),
    ]
    kustomization, _ = _edits(plan)
    assert set(kustomization) == {"resources", "patches"}


@pytest.mark.parametrize(
    ("overlay", "source", "action", "message"),
    [
        (None, _flux_source(target_namespace="other"), _action(), "targetNamespace"),
        (
            "namePrefix: prod-\nresources:\n  - ../../base\n",
            _flux_source(name="eu-prod-worker", name_prefix="eu-"),
            _action(name="eu-prod-worker"),
            "namePrefix",
        ),
        (None, _flux_source(name_suffix=" -x"), _action(), "nameSuffix"),
        (None, _flux_source(post_build="yes"), _action(), "post_build"),
        (None, _flux_source(patches=[{"patch": "x"}]), _action(), "patches"),
        (None, _flux_source(images=[{"name": "busybox", "newTag": "1"}]), _action(), "images"),
        (None, _flux_source(components=["../../components/x"]), _action(), "components"),
        (None, _flux_source(name="prod-worker"), _action(name="prod-worker"), "no exact target"),
        (None, _flux_source(target_namespace=None), _action(), "no exact target"),
    ],
    ids=[
        "namespace-mismatch",
        "flux-and-overlay-prefix",
        "invalid-suffix",
        "invalid-post-build",
        "flux-patches",
        "flux-images",
        "flux-components",
        "wrong-name",
        "flux-namespace-less-without-target-namespace",
    ],
)
def test_ambiguous_or_unsupported_flux_builds_fail_closed(
    tmp_path: Path,
    overlay: str | None,
    source: WorkloadSource,
    action: ActionPlanV3,
    message: str,
):
    files = _layout(overlay=overlay) if overlay is not None else _layout()

    with pytest.raises(KustomizePlanError, match=message):
        _plan(tmp_path, files, source, action, _KustomizeSemanticsRunner())


@pytest.mark.parametrize(
    ("worker", "passes"),
    [
        (WORKER, True),
        (WORKER.replace("cpu: 500m", "cpu: ${WORKER_CPU_LIMIT}"), False),
        (WORKER.replace("- name: worker\n", "- name: ${APP}\n"), False),
        (WORKER.replace("image: busybox:1.36.1", "image: busybox:${TAG}"), True),
        (
            WORKER.replace("cpu: 500m", "cpu: ${WORKER_CPU_LIMIT}").replace(
                "  labels:\n    app.kubernetes.io/name: worker\nspec:",
                "  labels:\n    app.kubernetes.io/name: worker\n"
                "  annotations:\n    kustomize.toolkit.fluxcd.io/substitute: disabled\nspec:",
                1,
            ),
            None,
        ),
    ],
    ids=["no-variables", "variable-limit", "variable-container-name", "variable-image", "substitution-disabled"],
)
def test_flux_post_build_variables_in_planned_fields_fail_closed(tmp_path: Path, worker: str, passes: bool | None):
    files = _layout(worker=worker)
    source = _flux_source(post_build=True)

    if passes:
        assert _plan(tmp_path, files, source, _action(), _KustomizeSemanticsRunner()).validation.passed
    elif passes is False:
        with pytest.raises(KustomizePlanError, match="postBuild"):
            _plan(tmp_path, files, source, _action(), _KustomizeSemanticsRunner())
    else:
        # Substitution is disabled: the literal "${...}" limit is not a quantity and the
        # render validation, not the postBuild guard, rejects it.
        with pytest.raises(KustomizePlanError) as excinfo:
            _plan(tmp_path, files, source, _action(), _KustomizeSemanticsRunner())
        assert "postBuild" not in str(excinfo.value)


def test_render_as_deployed_and_kustomize_rollback_use_the_flux_build(tmp_path: Path):
    """Rollback validation compares exact live identities, so it must render like Flux too."""
    renderer = KustomizeRenderer(expected_version="v5.8.3", runner=_KustomizeSemanticsRunner())
    snapshot = _snapshot(tmp_path, _layout())
    try:
        direct = render_as_deployed(renderer, snapshot, _flux_source())
    finally:
        snapshot.close()
    rollback = KustomizeRollbackRenderer(renderer).render(_layout(), _flux_source())

    for result in (direct, rollback):
        assert ("Deployment", TARGET_NAMESPACE, "worker") in {
            (resource.target.kind, resource.target.namespace, resource.target.name) for resource in result.resources
        }
