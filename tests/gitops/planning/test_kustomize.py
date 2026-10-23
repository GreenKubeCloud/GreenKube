from __future__ import annotations

import copy
import io
import zipfile
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
from greenkube.gitops.planning.kustomize import KustomizePlanError, KustomizePlanner
from greenkube.gitops.renderers.kustomize import KustomizeOutputError, KustomizeUnavailableError
from greenkube.gitops.renderers.sandbox import SandboxCommandNotFoundError, SandboxResult
from greenkube.gitops.snapshots import RepositorySnapshot, extract_archive

BASE_REVISION = "a" * 40
OVERLAY_PATH = "apps/payments/overlays/prod"

DEPLOYMENT = """\
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
          image: example/api:v1
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: "1"
              memory: 512Mi
        - name: sidecar
          image: example/sidecar:v1
          resources:
            requests:
              cpu: 50m
"""

SERVICE = """\
apiVersion: v1
kind: Service
metadata:
  name: api
  namespace: payments
spec:
  selector:
    app: api
"""

BASE_FILES = {
    f"{OVERLAY_PATH}/kustomization.yaml": "resources:\n  - ../../base\n",
    "apps/payments/base/kustomization.yaml": "resources:\n  - deployment.yaml\n  - service.yaml\n",
    "apps/payments/base/deployment.yaml": DEPLOYMENT,
    "apps/payments/base/service.yaml": SERVICE,
}


def _yaml() -> YAML:
    yaml = YAML(typ="safe")
    yaml.allow_duplicate_keys = False
    return yaml


def _source(path: str = OVERLAY_PATH, **overrides: Any) -> WorkloadSource:
    values: dict[str, Any] = {
        "id": "source-1",
        "cluster": "prod",
        "namespace": "payments",
        "workload_kind": "Deployment",
        "workload_name": "api",
        "provider_id": "github-main",
        "repo_url": "https://github.com/acme/gitops.git",
        "ref": "main",
        "revision": BASE_REVISION,
        "path": path,
        "controller_kind": ControllerKind.ARGOCD,
        "controller_name": "payments-prod",
        "controller_namespace": "argocd",
        "source_revision": BASE_REVISION,
        "renderer": RendererKind.KUSTOMIZE,
        "renderer_config": {"overlay_path": path},
        "confidence": 0.98,
        "status": BindingStatus.RESOLVED,
        "evidence": {},
    }
    values.update(overrides)
    return WorkloadSource(**values)


def _action(*, cpu: str = "300m", memory: str = "256Mi") -> ActionPlanV3:
    return ActionPlanV3(
        action_id="act-1",
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
                resources=ContainerResources(requests={"cpu": cpu, "memory": memory}),
            )
        ],
        constraints={},
        evidence_ref="recommendation/42",
    )


def _snapshot(
    tmp_path: Path,
    files: Mapping[str, str | bytes],
    *,
    commit_sha: str = BASE_REVISION,
) -> RepositorySnapshot:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, mode="w") as repository:
        for path, content in files.items():
            repository.writestr(path, content)
    return extract_archive(
        archive.getvalue(),
        provider_id="github-main",
        repo_url="https://github.com/acme/gitops.git",
        commit_sha=commit_sha,
        workspace_parent=tmp_path,
    )


class _FakeKustomizeRunner:
    def __init__(
        self,
        *,
        unrelated_change: bool = False,
        ignore_patches: bool = False,
        duplicate_target: bool = False,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.unrelated_change = unrelated_change
        self.ignore_patches = ignore_patches
        self.duplicate_target = duplicate_target

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
        arguments = tuple(argv)
        self.calls.append(arguments)
        if arguments[1:] == ("version",):
            return SandboxResult(returncode=0, stdout=b"v5.8.3\n", stderr=b"")

        overlay = workspace / arguments[2]
        resources = self._load_resources(overlay)
        config = _yaml().load(self._kustomization_path(overlay).read_text(encoding="utf-8")) or {}
        if not self.ignore_patches:
            for entry in config.get("patches", []):
                patch = _yaml().load((overlay / entry["path"]).read_text(encoding="utf-8"))
                self._apply_patch(resources, patch, entry["target"])

        if self.unrelated_change and config.get("patches"):
            service = next(resource for resource in resources if resource.get("kind") == "Service")
            service.setdefault("metadata", {}).setdefault("labels", {})["unexpected"] = "change"
        if self.duplicate_target:
            target = next(resource for resource in resources if resource.get("kind") == "Deployment")
            resources.append(copy.deepcopy(target))

        stream = io.StringIO()
        YAML(typ="safe").dump_all(resources, stream)
        return SandboxResult(returncode=0, stdout=stream.getvalue().encode(), stderr=b"")

    def _load_resources(self, directory: Path) -> list[dict[str, Any]]:
        config = _yaml().load(self._kustomization_path(directory).read_text(encoding="utf-8")) or {}
        resources: list[dict[str, Any]] = []
        for reference in config.get("resources", []):
            path = (directory / reference).resolve()
            if path.is_dir():
                resources.extend(self._load_resources(path))
            else:
                resources.extend(_yaml().load_all(path.read_text(encoding="utf-8")))
        return resources

    @staticmethod
    def _kustomization_path(directory: Path) -> Path:
        for name in ("kustomization.yaml", "kustomization.yml", "Kustomization"):
            candidate = directory / name
            if candidate.is_file():
                return candidate
        raise AssertionError(f"{directory} has no Kustomization file")

    @staticmethod
    def _apply_patch(
        resources: list[dict[str, Any]],
        patch: dict[str, Any],
        target: dict[str, str],
    ) -> None:
        match = next(
            resource
            for resource in resources
            if resource["apiVersion"]
            == (f"{target['group']}/{target['version']}" if target["group"] else target["version"])
            and resource["kind"] == target["kind"]
            and resource["metadata"]["name"] == target["name"]
            and resource["metadata"]["namespace"] == target["namespace"]
        )
        patch_containers = patch["spec"]["template"]["spec"]["containers"]
        containers = match["spec"]["template"]["spec"]["containers"]
        for patch_container in patch_containers:
            container = next(item for item in containers if item["name"] == patch_container["name"])
            current = container.setdefault("resources", {}).setdefault("requests", {})
            current.update(patch_container["resources"]["requests"])
        by_name = {container["name"]: container for container in containers}
        patch_order = [container["name"] for container in patch_containers]
        match["spec"]["template"]["spec"]["containers"] = [by_name[name] for name in patch_order] + [
            container for container in containers if container["name"] not in patch_order
        ]


def test_plan_adds_managed_patch_and_overlay_entry_deterministically(tmp_path: Path):
    snapshot = _snapshot(tmp_path, BASE_FILES)
    runner = _FakeKustomizeRunner()

    try:
        planner = KustomizePlanner(runner=runner)
        plan = planner.plan(_action(), _source(), snapshot, BASE_REVISION)
        repeated = planner.plan(_action(), _source(), snapshot, BASE_REVISION)

        assert plan.validation.passed
        assert plan.validation.renderer is RendererKind.KUSTOMIZE
        assert plan.validation.target_changed
        assert not plan.validation.unrelated_changes
        assert len(plan.files) == 2
        assert plan.digest == repeated.digest
        assert plan.diff == repeated.diff
        assert plan.rendered_diff == repeated.rendered_diff

        kustomization_edit = next(edit for edit in plan.files if edit.path.endswith("kustomization.yaml"))
        patch_edit = next(edit for edit in plan.files if "greenkube-patches/" in edit.path)
        assert kustomization_edit.operation == "update"
        assert patch_edit.operation == "create"
        assert patch_edit.original_sha is None
        assert patch_edit.path.startswith(f"{OVERLAY_PATH}/greenkube-patches/greenkube-deployment-api-")
        assert patch_edit.path.endswith(".yaml")

        patch = _yaml().load(patch_edit.content or "")
        assert patch["apiVersion"] == "apps/v1"
        assert patch["kind"] == "Deployment"
        assert patch["metadata"] == {"name": "api", "namespace": "payments"}
        assert patch["spec"]["template"]["spec"]["containers"] == [
            {
                "name": "api",
                "resources": {"requests": {"cpu": "300m", "memory": "256Mi"}},
            }
        ]

        kustomization = _yaml().load(kustomization_edit.content or "")
        assert len(kustomization["patches"]) == 1
        assert any(
            line.lstrip().startswith("- path: greenkube-patches/")
            for line in (kustomization_edit.content or "").splitlines()
        )
        entry = kustomization["patches"][0]
        assert entry["path"] == "greenkube-patches/" + Path(patch_edit.path).name
        assert entry["target"] == {
            "group": "apps",
            "version": "v1",
            "kind": "Deployment",
            "name": "api",
            "namespace": "payments",
        }
        assert plan.diff.count("--- ") == 2
        assert '"cpu": "100m"' in plan.rendered_diff
        assert '"cpu": "300m"' in plan.rendered_diff
        assert '"Service"' not in plan.rendered_diff
        assert not (snapshot.workspace / patch_edit.path).exists()
        assert (snapshot.workspace / kustomization_edit.path).read_text(encoding="utf-8") == BASE_FILES[
            kustomization_edit.path
        ]
    finally:
        snapshot.close()


def test_multi_container_patch_preserves_rendered_container_order(tmp_path: Path):
    files = dict(BASE_FILES)
    files["apps/payments/base/deployment.yaml"] = DEPLOYMENT.replace(
        "        - name: api\n          image: example/api:v1",
        "        - name: web\n          image: example/api:v1",
    ).replace(
        "        - name: sidecar\n          image: example/sidecar:v1",
        "        - name: telemetry-sidecar\n          image: example/sidecar:v1",
    )
    snapshot = _snapshot(tmp_path, files)
    action = ActionPlanV3(
        action_id="multi-container",
        recommendation_ids=[42, 43],
        target=ResourceTarget(
            api_version="apps/v1",
            kind="Deployment",
            namespace="payments",
            name="api",
        ),
        operations=[
            SetContainerResources(
                container="telemetry-sidecar",
                resources=ContainerResources(requests={"cpu": "200m"}),
            ),
            SetContainerResources(
                container="web",
                resources=ContainerResources(requests={"cpu": "200m"}),
            ),
        ],
        constraints={},
        evidence_ref="recommendation/42/43",
    )

    try:
        plan = KustomizePlanner(runner=_FakeKustomizeRunner()).plan(action, _source(), snapshot, BASE_REVISION)
        patch_edit = next(edit for edit in plan.files if "greenkube-patches/" in edit.path)
        patch = _yaml().load(patch_edit.content or "")

        assert plan.validation.passed
        assert [item["name"] for item in patch["spec"]["template"]["spec"]["containers"]] == [
            "web",
            "telemetry-sidecar",
        ]
    finally:
        snapshot.close()


def test_flux_target_namespace_matching_overlay_namespace_is_supported(tmp_path: Path):
    files = dict(BASE_FILES)
    files[f"{OVERLAY_PATH}/kustomization.yaml"] = "namespace: payments\nresources:\n  - ../../base\n"
    source = _source(
        controller_kind=ControllerKind.FLUX_KUSTOMIZATION,
        controller_name="payments",
        controller_namespace="flux-system",
        renderer_config={"overlay_path": OVERLAY_PATH, "target_namespace": "payments"},
    )
    snapshot = _snapshot(tmp_path, files)

    try:
        plan = KustomizePlanner(runner=_FakeKustomizeRunner()).plan(_action(), source, snapshot, BASE_REVISION)
    finally:
        snapshot.close()

    assert plan.validation.passed
    assert plan.validation.renderer is RendererKind.KUSTOMIZE


def test_flux_target_namespace_must_match_the_overlay_namespace(tmp_path: Path):
    files = dict(BASE_FILES)
    files[f"{OVERLAY_PATH}/kustomization.yaml"] = "namespace: payments\nresources:\n  - ../../base\n"
    source = _source(
        controller_kind=ControllerKind.FLUX_KUSTOMIZATION,
        controller_name="payments",
        controller_namespace="flux-system",
        renderer_config={"overlay_path": OVERLAY_PATH, "target_namespace": "other"},
    )
    snapshot = _snapshot(tmp_path, files)

    try:
        with pytest.raises(KustomizePlanError, match="targetNamespace"):
            KustomizePlanner(runner=_FakeKustomizeRunner()).plan(_action(), source, snapshot, BASE_REVISION)
    finally:
        snapshot.close()


def test_plan_id_is_repeatable_per_action_binding_and_base_revision(tmp_path: Path):
    next_revision = "c" * 40
    first_snapshot = _snapshot(tmp_path, BASE_FILES)
    next_snapshot = _snapshot(tmp_path, BASE_FILES, commit_sha=next_revision)

    try:
        planner = KustomizePlanner(runner=_FakeKustomizeRunner())
        action = _action()
        source = _source()
        first = planner.plan(action, source, first_snapshot, BASE_REVISION)
        repeated = planner.plan(action, source, first_snapshot, BASE_REVISION)
        next_revision_plan = planner.plan(action, source, next_snapshot, next_revision)
        other_binding = planner.plan(action, _source(id="source-2"), first_snapshot, BASE_REVISION)

        assert first.plan_id.startswith("pln-")
        assert first.plan_id == repeated.plan_id
        assert first.plan_id != next_revision_plan.plan_id
        assert first.plan_id != other_binding.plan_id
    finally:
        first_snapshot.close()
        next_snapshot.close()


def test_overlay_accepts_supported_kustomization_file_names(tmp_path: Path):
    files = dict(BASE_FILES)
    original_path = f"{OVERLAY_PATH}/kustomization.yaml"
    files[f"{OVERLAY_PATH}/Kustomization"] = files.pop(original_path)
    snapshot = _snapshot(tmp_path, files)

    try:
        source = _source(
            renderer_config={
                "overlay_path": OVERLAY_PATH,
                "kustomization_path": original_path,
            }
        )
        plan = KustomizePlanner(runner=_FakeKustomizeRunner()).plan(
            _action(),
            source,
            snapshot,
            BASE_REVISION,
        )

        assert plan.validation.passed
        assert any(edit.path == f"{OVERLAY_PATH}/Kustomization" for edit in plan.files)
    finally:
        snapshot.close()


def test_existing_managed_patch_is_updated_without_duplicate_entry(tmp_path: Path):
    first_snapshot = _snapshot(tmp_path, BASE_FILES)
    try:
        first_plan = KustomizePlanner(runner=_FakeKustomizeRunner()).plan(
            _action(),
            _source(),
            first_snapshot,
            BASE_REVISION,
        )
    finally:
        first_snapshot.close()

    files = dict(BASE_FILES)
    for edit in first_plan.files:
        files[edit.path] = edit.content or ""
    patch_edit = next(edit for edit in first_plan.files if "greenkube-patches/" in edit.path)
    old_patch = _yaml().load(files[patch_edit.path])
    old_patch["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "200m"
    stream = io.StringIO()
    YAML(typ="safe").dump(old_patch, stream)
    files[patch_edit.path] = stream.getvalue()

    snapshot = _snapshot(tmp_path, files)
    try:
        plan = KustomizePlanner(runner=_FakeKustomizeRunner()).plan(
            _action(),
            _source(),
            snapshot,
            BASE_REVISION,
        )

        assert plan.validation.passed
        assert len(plan.files) == 1
        assert plan.files[0].path == patch_edit.path
        assert plan.files[0].operation == "update"
        kustomization = _yaml().load((snapshot.workspace / f"{OVERLAY_PATH}/kustomization.yaml").read_text())
        assert len(kustomization["patches"]) == 1
        assert '"cpu": "200m"' in plan.rendered_diff
        assert '"cpu": "300m"' in plan.rendered_diff
    finally:
        snapshot.close()


def test_existing_managed_patch_preserves_other_request_fields_across_plans(tmp_path: Path):
    first_snapshot = _snapshot(tmp_path, BASE_FILES)
    try:
        first_plan = KustomizePlanner(runner=_FakeKustomizeRunner()).plan(
            _action(cpu="200m", memory="256Mi"),
            _source(),
            first_snapshot,
            BASE_REVISION,
        )
    finally:
        first_snapshot.close()

    files = dict(BASE_FILES)
    for edit in first_plan.files:
        files[edit.path] = edit.content or ""
    second_action = ActionPlanV3(
        action_id="act-memory-followup",
        recommendation_ids=[43],
        target=_action().target,
        operations=[
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"memory": "96Mi"}),
            )
        ],
        constraints={},
        evidence_ref="recommendation/43",
    )
    snapshot = _snapshot(tmp_path, files)
    try:
        plan = KustomizePlanner(runner=_FakeKustomizeRunner()).plan(
            second_action,
            _source(),
            snapshot,
            BASE_REVISION,
        )

        assert plan.validation.passed
        assert len(plan.files) == 1
        managed_patch = _yaml().load(plan.files[0].content or "")
        requests = managed_patch["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]
        assert requests == {"cpu": "200m", "memory": "96Mi"}
    finally:
        snapshot.close()


def test_shared_base_is_refused_before_rendering(tmp_path: Path):
    files = {
        **BASE_FILES,
        "apps/staging/kustomization.yaml": "resources:\n  - ../payments/base\n",
        "apps/dev/kustomization.yaml": "resources:\n  - ../payments/base\n",
    }
    snapshot = _snapshot(tmp_path, files)
    runner = _FakeKustomizeRunner()

    try:
        with pytest.raises(KustomizePlanError, match="shared base"):
            KustomizePlanner(runner=runner).plan(
                _action(),
                _source("apps/payments/base"),
                snapshot,
                BASE_REVISION,
            )

        assert runner.calls == []
    finally:
        snapshot.close()


@pytest.mark.parametrize(
    ("overlay", "message"),
    [
        ("resources:\n  - https://example.test/repo//base\n", "remote"),
        ("resources:\n  - ../../base\ncomponents:\n  - ../../shared\n", "component"),
        ("resources:\n  - ../../base\nhelmCharts:\n  - name: demo\n", "Helm"),
        (
            "apiVersion: kustomize.config.k8s.io/v1alpha1\nkind: Kustomization\nresources:\n  - ../../base\n",
            "schema",
        ),
        ("resources:\n  - ../../base\nunknownFeature: true\n", "schema"),
        ("resources:\n  - ../../base\npatchesStrategicMerge:\n  - patch.yaml\n", "legacy"),
    ],
)
def test_unsupported_overlay_features_fail_closed(tmp_path: Path, overlay: str, message: str):
    files = dict(BASE_FILES)
    files[f"{OVERLAY_PATH}/kustomization.yaml"] = overlay
    snapshot = _snapshot(tmp_path, files)
    runner = _FakeKustomizeRunner()

    try:
        with pytest.raises(KustomizePlanError, match=message):
            KustomizePlanner(runner=runner).plan(_action(), _source(), snapshot, BASE_REVISION)

        assert runner.calls == []
    finally:
        snapshot.close()


def test_render_diff_rejects_changes_to_unrelated_resources(tmp_path: Path):
    snapshot = _snapshot(tmp_path, BASE_FILES)

    try:
        with pytest.raises(KustomizePlanError, match="unrelated resource change"):
            KustomizePlanner(runner=_FakeKustomizeRunner(unrelated_change=True)).plan(
                _action(),
                _source(),
                snapshot,
                BASE_REVISION,
            )
    finally:
        snapshot.close()


def test_request_limits_are_the_only_supported_kustomize_edit(tmp_path: Path):
    snapshot = _snapshot(tmp_path, BASE_FILES)
    action = ActionPlanV3(
        action_id="act-1",
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
                resources=ContainerResources(requests={"cpu": "300m"}, limits={"cpu": "800m"}),
            )
        ],
        constraints={},
        evidence_ref="recommendation/42",
    )

    try:
        with pytest.raises(KustomizePlanError, match="requests only"):
            KustomizePlanner(runner=_FakeKustomizeRunner()).plan(
                action,
                _source(),
                snapshot,
                BASE_REVISION,
            )
    finally:
        snapshot.close()


def test_missing_pinned_kustomize_binary_fails_without_raw_fallback(tmp_path: Path):
    class _UnavailableRunner:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def run(self, argv: Sequence[str], **_: Any) -> SandboxResult:
            self.calls.append(tuple(argv))
            raise SandboxCommandNotFoundError("kustomize is not installed")

    snapshot = _snapshot(tmp_path, BASE_FILES)
    runner = _UnavailableRunner()

    try:
        with pytest.raises(KustomizeUnavailableError, match="unavailable"):
            KustomizePlanner(runner=runner).plan(_action(), _source(), snapshot, BASE_REVISION)

        assert runner.calls == [("kustomize", "version")]
        assert (snapshot.workspace / f"{OVERLAY_PATH}/kustomization.yaml").read_text(encoding="utf-8") == BASE_FILES[
            f"{OVERLAY_PATH}/kustomization.yaml"
        ]
    finally:
        snapshot.close()


def test_ambiguous_target_render_is_rejected(tmp_path: Path):
    snapshot = _snapshot(tmp_path, BASE_FILES)

    try:
        with pytest.raises(KustomizeOutputError, match="Kustomize output"):
            KustomizePlanner(runner=_FakeKustomizeRunner(duplicate_target=True)).plan(
                _action(),
                _source(),
                snapshot,
                BASE_REVISION,
            )
    finally:
        snapshot.close()


def test_a_render_that_ignores_the_patch_is_not_accepted(tmp_path: Path):
    snapshot = _snapshot(tmp_path, BASE_FILES)

    try:
        with pytest.raises(KustomizePlanError, match="render validation"):
            KustomizePlanner(runner=_FakeKustomizeRunner(ignore_patches=True)).plan(
                _action(),
                _source(),
                snapshot,
                BASE_REVISION,
            )
    finally:
        snapshot.close()
