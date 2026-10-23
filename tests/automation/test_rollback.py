from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from greenkube.automation.rollback import (
    KustomizeRollbackRenderer,
    RawManifestRollbackRenderer,
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
    DeleteResource,
    FileEdit,
    FilePlan,
    Plan,
    PlanStatus,
    PreApplyWorkloadBaseline,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    ValidationResult,
    WorkloadSource,
)
from greenkube.gitops.renderers.kustomize import KustomizeRenderer
from greenkube.gitops.renderers.raw import RawRenderResult, render_raw
from greenkube.gitops.renderers.sandbox import SandboxResult
from greenkube.gitops.snapshots.models import RepositorySnapshot, SnapshotFile, SnapshotMetadata

PLAN_ID = "plan-original"
PLAN_DIGEST = f"sha256:{'d' * 64}"
OUTPUT_DIGEST = f"sha256:{'e' * 64}"
ORIGINAL_BASE = "a" * 40
CURRENT_BASE = "b" * 40
MANIFEST_PATH = "apps/payments/deployment.yaml"
CREATED_PATH = "apps/payments/generated.txt"
UNRELATED_PATH = "apps/other/README.md"
BASE_CONTENT = """apiVersion: apps/v1
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
"""
FORWARD_CONTENT = BASE_CONTENT.replace("cpu: 100m", "cpu: 300m")


def _blob_sha(content: str, *, revision: str = ORIGINAL_BASE) -> str:
    payload = content.encode("utf-8")
    digest = hashlib.sha256 if len(revision) == 64 else hashlib.sha1
    return digest(b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload).hexdigest()


def _source(*, renderer: RendererKind = RendererKind.RAW) -> WorkloadSource:
    return WorkloadSource(
        id="source-1",
        cluster="prod",
        namespace="payments",
        workload_kind="Deployment",
        workload_name="api",
        provider_id="github-main",
        repo_url="https://github.com/acme/gitops.git",
        ref="main",
        revision=ORIGINAL_BASE,
        path="apps/payments",
        controller_kind=ControllerKind.ARGOCD,
        controller_name="payments-prod",
        controller_namespace="argocd",
        source_revision=ORIGINAL_BASE,
        renderer=renderer,
        renderer_config={},
        confidence=1,
        status=BindingStatus.RESOLVED,
        evidence={},
    )


def _action(*, operation: Any | None = None) -> ActionPlanV3:
    return ActionPlanV3(
        action_id=PLAN_ID,
        recommendation_ids=[42],
        target=ResourceTarget(
            api_version="apps/v1",
            kind="Deployment",
            namespace="payments",
            name="api",
        ),
        operations=[
            operation
            if operation is not None
            else SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"cpu": "300m"}),
            )
        ],
        evidence_ref="recommendation/42",
    )


def _plan(
    edits: list[FileEdit],
    *,
    renderer: RendererKind = RendererKind.RAW,
    operation: Any | None = None,
) -> Plan:
    validation = ValidationResult(
        target_changed=True,
        unrelated_changes=False,
        requests_within_limits=True,
        renderer=renderer,
        output_digest=OUTPUT_DIGEST,
    )
    file_plan = FilePlan(
        plan_id=PLAN_ID,
        files=edits,
        diff="",
        rendered_diff="",
        base_revision=ORIGINAL_BASE,
        source_revision=ORIGINAL_BASE,
        renderer=renderer,
        validation=validation,
        digest=PLAN_DIGEST,
    )
    action = _action(operation=operation)
    return Plan(
        plan_id=PLAN_ID,
        recommendation_ids=action.recommendation_ids,
        source=_source(renderer=renderer),
        action=action,
        file_plan=file_plan,
        status=PlanStatus.APPLIED,
        digest=PLAN_DIGEST,
    )


def test_build_inverse_restores_original_and_deletes_only_forward_created_paths() -> None:
    plan = _plan(
        [
            FileEdit(
                path=MANIFEST_PATH,
                operation="update",
                original_sha=_blob_sha(BASE_CONTENT),
                content=FORWARD_CONTENT,
            ),
            FileEdit(path=CREATED_PATH, operation="create", content="generated\n"),
        ]
    )
    original = {MANIFEST_PATH: BASE_CONTENT}
    current = {
        MANIFEST_PATH: FORWARD_CONTENT,
        CREATED_PATH: "generated\n",
        UNRELATED_PATH: "keep me\n",
    }

    inverse = build_inverse_plan(plan, original, current, CURRENT_BASE)
    repeated = build_inverse_plan(plan, original, current, CURRENT_BASE)

    assert [(edit.path, edit.operation, edit.content) for edit in inverse.files] == [
        (MANIFEST_PATH, "update", BASE_CONTENT),
        (CREATED_PATH, "delete", None),
    ]
    assert inverse.files[0].original_sha == _blob_sha(FORWARD_CONTENT, revision=CURRENT_BASE)
    assert inverse.files[1].original_sha == _blob_sha("generated\n", revision=CURRENT_BASE)
    assert inverse.plan_id == repeated.plan_id
    assert inverse.digest == repeated.digest
    assert inverse.base_revision == CURRENT_BASE
    assert inverse.original_plan_id == PLAN_ID
    assert inverse.original_digest == PLAN_DIGEST
    assert inverse.rendered_before_digest != inverse.rendered_after_digest
    assert inverse.digest.startswith("sha256:")
    assert current[MANIFEST_PATH] == FORWARD_CONTENT
    assert current[CREATED_PATH] == "generated\n"
    assert UNRELATED_PATH not in {edit.path for edit in inverse.files}


def test_forward_created_path_that_is_still_absent_produces_no_file_edit() -> None:
    plan = _plan([FileEdit(path=CREATED_PATH, operation="create", content="generated\n")])

    inverse = build_inverse_plan(plan, {}, {MANIFEST_PATH: BASE_CONTENT}, CURRENT_BASE)

    assert inverse.files == ()
    assert inverse.rendered_before_digest == inverse.rendered_after_digest


@pytest.mark.parametrize(
    "current",
    [
        {},
        {MANIFEST_PATH: BASE_CONTENT.replace("100m", "200m")},
    ],
    ids=["deleted-source-path", "changed-source-path"],
)
def test_update_requires_current_path_to_match_forward_output_exactly(current: dict[str, str]) -> None:
    plan = _plan(
        [
            FileEdit(
                path=MANIFEST_PATH,
                operation="update",
                original_sha=_blob_sha(BASE_CONTENT),
                content=FORWARD_CONTENT,
            )
        ]
    )

    with pytest.raises(RollbackConflictError):
        build_inverse_plan(plan, {MANIFEST_PATH: BASE_CONTENT}, current, CURRENT_BASE)


def test_created_path_with_changed_content_is_rejected() -> None:
    plan = _plan([FileEdit(path=CREATED_PATH, operation="create", content="forward\n")])

    with pytest.raises(RollbackConflictError, match="created"):
        build_inverse_plan(plan, {}, {CREATED_PATH: "someone else's file\n"}, CURRENT_BASE)


def test_original_immutable_content_must_match_forward_original_sha() -> None:
    plan = _plan(
        [
            FileEdit(
                path=MANIFEST_PATH,
                operation="update",
                original_sha=_blob_sha(BASE_CONTENT),
                content=FORWARD_CONTENT,
            )
        ]
    )

    with pytest.raises(RollbackConflictError, match="original"):
        build_inverse_plan(
            plan,
            {MANIFEST_PATH: BASE_CONTENT.replace("100m", "150m")},
            {MANIFEST_PATH: FORWARD_CONTENT},
            CURRENT_BASE,
        )


def test_inverse_is_rejected_if_it_changes_an_unrelated_rendered_resource() -> None:
    original = (
        BASE_CONTENT
        + """---
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
  namespace: payments
data:
  mode: old
"""
    )
    forward = original.replace("cpu: 100m", "cpu: 300m").replace("mode: old", "mode: new")
    plan = _plan(
        [
            FileEdit(
                path=MANIFEST_PATH,
                operation="update",
                original_sha=_blob_sha(original),
                content=forward,
            )
        ]
    )

    with pytest.raises(RollbackValidationError, match="unrelated"):
        build_inverse_plan(plan, {MANIFEST_PATH: original}, {MANIFEST_PATH: forward}, CURRENT_BASE)


def test_unsupported_forward_action_is_rejected_explicitly() -> None:
    plan = _plan(
        [
            FileEdit(
                path=MANIFEST_PATH,
                operation="update",
                original_sha=_blob_sha(BASE_CONTENT),
                content=FORWARD_CONTENT,
            )
        ],
        operation=DeleteResource(),
    )

    with pytest.raises(UnsupportedRollbackError, match="operation"):
        build_inverse_plan(
            plan,
            {MANIFEST_PATH: BASE_CONTENT},
            {MANIFEST_PATH: FORWARD_CONTENT},
            CURRENT_BASE,
        )


def test_forward_file_delete_is_not_guessed() -> None:
    plan = _plan(
        [
            FileEdit(
                path=MANIFEST_PATH,
                operation="delete",
                original_sha=_blob_sha(BASE_CONTENT),
            )
        ]
    )

    with pytest.raises(UnsupportedRollbackError, match="delete"):
        build_inverse_plan(plan, {MANIFEST_PATH: BASE_CONTENT}, {}, CURRENT_BASE)


def test_unsupported_renderer_requires_an_explicit_exact_adapter() -> None:
    plan = _plan(
        [
            FileEdit(
                path=MANIFEST_PATH,
                operation="update",
                original_sha=_blob_sha(BASE_CONTENT),
                content=FORWARD_CONTENT,
            )
        ],
        renderer=RendererKind.KUSTOMIZE,
    )

    with pytest.raises(UnsupportedRollbackError, match="Kustomize"):
        build_inverse_plan(
            plan,
            {MANIFEST_PATH: BASE_CONTENT},
            {MANIFEST_PATH: FORWARD_CONTENT},
            CURRENT_BASE,
        )


def test_flux_helm_release_rollback_restores_only_the_exact_forward_manifest() -> None:
    original_release = """\
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: payments
  namespace: flux-system
spec:
  values:
    app:
      resources:
        requests:
          cpu: 100m
"""
    forward_release = original_release.replace("cpu: 100m", "cpu: 300m")
    release_path = "apps/payments/helmrelease.yaml"
    source = _source(renderer=RendererKind.HELM_RELEASE).model_copy(
        update={
            "controller_kind": ControllerKind.FLUX_HELM_RELEASE,
            "source_revision": f"main@sha1:{ORIGINAL_BASE}",
            "renderer_config": {
                "manifest_source": {
                    "provider_id": "github-main",
                    "repo_url": "https://github.com/acme/gitops.git",
                    "ref": "main",
                    "path": "apps/payments",
                    "revision": ORIGINAL_BASE,
                }
            },
            "evidence": {
                "source_ref": {"kind": "GitRepository", "name": "gitops", "namespace": "flux-system"},
                "artifact_revision": f"main@sha1:{ORIGINAL_BASE}",
            },
        }
    )
    plan = _plan(
        [
            FileEdit(
                path=release_path,
                operation="update",
                original_sha=_blob_sha(original_release),
                content=forward_release,
            )
        ],
        renderer=RendererKind.HELM_RELEASE,
    ).model_copy(
        update={
            "source": source,
            "pre_apply_baseline": PreApplyWorkloadBaseline(
                workload_uid="uid-api",
                generation=7,
                observed_generation=7,
                container_requests={
                    "api": ContainerRequestBaseline(cpu_millicores=100, memory_bytes=None),
                    "sidecar": ContainerRequestBaseline(cpu_millicores=25, memory_bytes=None),
                },
            ),
        }
    )
    renderer = _FakeFluxHelmReleaseRollbackRenderer()
    forward_digest = renderer.render({release_path: forward_release}, source).output_digest
    file_plan = plan.file_plan
    assert file_plan is not None
    plan = plan.model_copy(
        update={
            "file_plan": file_plan.model_copy(
                update={
                    "validation": file_plan.validation.model_copy(update={"output_digest": forward_digest}),
                }
            )
        }
    )
    current_source = source.model_copy(
        update={
            "revision": CURRENT_BASE,
            "source_revision": f"main@sha1:{CURRENT_BASE}",
            "renderer_config": {
                **source.renderer_config,
                "manifest_source": {
                    **source.renderer_config["manifest_source"],
                    "revision": CURRENT_BASE,
                },
                "post_renderers": [{"managed": True}],
            },
            "evidence": {
                **source.evidence,
                "artifact_revision": f"main@sha1:{CURRENT_BASE}",
            },
        }
    )

    inverse = build_inverse_plan(
        plan,
        {release_path: original_release},
        {release_path: forward_release},
        CURRENT_BASE,
        renderer=renderer,
        current_source=current_source,
    )

    assert [(edit.path, edit.operation, edit.content) for edit in inverse.files] == [
        (release_path, "update", original_release),
    ]
    assert inverse.files[0].original_sha == _blob_sha(forward_release, revision=CURRENT_BASE)
    assert inverse.rendered_before_digest == forward_digest


class _FakeFluxHelmReleaseRollbackRenderer:
    renderer = RendererKind.HELM_RELEASE
    supports_exact_target_rollback = True

    def render(self, files: Mapping[str, str | bytes], source: WorkloadSource) -> RawRenderResult:
        del source
        release = files["apps/payments/helmrelease.yaml"]
        release_text = release.decode("utf-8") if isinstance(release, bytes) else release
        cpu = next(line.split(":", 1)[1].strip() for line in release_text.splitlines() if "cpu:" in line)
        manifest = f"""\
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
            requests:
              cpu: {cpu}
        - name: sidecar
          resources:
            requests:
              cpu: 25m
"""
        return replace(render_raw({"rendered/helm-release.yaml": manifest}), renderer=RendererKind.HELM_RELEASE)


def test_flux_helm_release_rollback_rejects_a_candidate_that_misses_the_persisted_baseline() -> None:
    release_path = "apps/payments/helmrelease.yaml"
    original_release = """\
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: payments
  namespace: flux-system
spec:
  values:
    app:
      resources:
        requests:
          cpu: 100m
"""
    forward_release = original_release.replace("cpu: 100m", "cpu: 300m")
    source = _source(renderer=RendererKind.HELM_RELEASE).model_copy(
        update={
            "controller_kind": ControllerKind.FLUX_HELM_RELEASE,
            "source_revision": f"main@sha1:{ORIGINAL_BASE}",
            "renderer_config": {
                "manifest_source": {
                    "provider_id": "github-main",
                    "repo_url": "https://github.com/acme/gitops.git",
                    "ref": "main",
                    "path": "apps/payments",
                    "revision": ORIGINAL_BASE,
                }
            },
            "evidence": {
                "source_ref": {"kind": "GitRepository", "name": "gitops", "namespace": "flux-system"},
                "artifact_revision": f"main@sha1:{ORIGINAL_BASE}",
            },
        }
    )
    plan = _plan(
        [
            FileEdit(
                path=release_path,
                operation="update",
                original_sha=_blob_sha(original_release),
                content=forward_release,
            )
        ],
        renderer=RendererKind.HELM_RELEASE,
    ).model_copy(
        update={
            "source": source,
            "pre_apply_baseline": PreApplyWorkloadBaseline(
                workload_uid="uid-api",
                generation=7,
                observed_generation=7,
                container_requests={
                    "api": ContainerRequestBaseline(cpu_millicores=90, memory_bytes=None),
                    "sidecar": ContainerRequestBaseline(cpu_millicores=25, memory_bytes=None),
                },
            ),
        }
    )
    renderer = _FakeFluxHelmReleaseRollbackRenderer()
    file_plan = plan.file_plan
    assert file_plan is not None
    plan = plan.model_copy(
        update={
            "file_plan": file_plan.model_copy(
                update={
                    "validation": file_plan.validation.model_copy(
                        update={"output_digest": renderer.render({release_path: forward_release}, source).output_digest}
                    ),
                }
            )
        }
    )
    current_source = source.model_copy(
        update={
            "revision": CURRENT_BASE,
            "source_revision": f"main@sha1:{CURRENT_BASE}",
            "renderer_config": {
                **source.renderer_config,
                "manifest_source": {**source.renderer_config["manifest_source"], "revision": CURRENT_BASE},
            },
        }
    )

    with pytest.raises(RollbackValidationError, match="baseline"):
        build_inverse_plan(
            plan,
            {release_path: original_release},
            {release_path: forward_release},
            CURRENT_BASE,
            renderer=renderer,
            current_source=current_source,
        )


_HELM_VALUES_PATH = "apps/payments/values.yaml"
_HELM_ORIGINAL_VALUES = """\
app:
  resources:
    requests:
      cpu: 80m
"""
_HELM_FORWARD_VALUES = """\
app:
  resources:
    requests:
      cpu: 300m
"""
_HELM_BASELINE_VALUES = """\
app:
  resources:
    requests:
      cpu: 100m
"""


class _FakeHelmValuesRollbackRenderer:
    renderer = RendererKind.HELM_VALUES
    supports_exact_target_rollback = True

    def __init__(self, *, unrelated_output: bool = False) -> None:
        self.unrelated_output = unrelated_output
        self.mappings: list[object] = []

    def plan_inverse(
        self,
        plan: Plan,
        files: Mapping[str, str | bytes],
        source: WorkloadSource,
        base_revision: str,
    ) -> list[FileEdit]:
        mapping = plan.source.renderer_config["values_mapping"]
        self.mappings.append(mapping)
        baseline = plan.pre_apply_baseline
        assert baseline is not None
        cpu_millicores = baseline.container_requests["api"].cpu_millicores
        assert cpu_millicores is not None
        assert source.revision == base_revision
        return [
            FileEdit(
                path=_HELM_VALUES_PATH,
                operation="update",
                original_sha=_blob_sha(
                    _content_text(files[_HELM_VALUES_PATH]),
                    revision=base_revision,
                ),
                content=_HELM_BASELINE_VALUES.replace("100m", f"{cpu_millicores}m"),
            )
        ]

    def render(self, files: Mapping[str, str | bytes], source: WorkloadSource) -> RawRenderResult:
        del source
        values = _content_text(files[_HELM_VALUES_PATH])
        cpu = next(line.split(":", 1)[1].strip() for line in values.splitlines() if "cpu:" in line)
        manifest = BASE_CONTENT.replace("cpu: 100m", f"cpu: {cpu}")
        resources = {MANIFEST_PATH: manifest}
        if self.unrelated_output:
            resources["apps/payments/configmap.yaml"] = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
  namespace: payments
data:
  mode: changed
"""
        return replace(render_raw(resources), renderer=RendererKind.HELM_VALUES)


def _content_text(content: str | bytes) -> str:
    return content.decode("utf-8") if isinstance(content, bytes) else content


def _helm_values_rollback_plan(*, include_baseline: bool = True) -> tuple[Plan, dict[str, str], dict[str, str]]:
    source_plan = _plan(
        [
            FileEdit(
                path=_HELM_VALUES_PATH,
                operation="update",
                original_sha=_blob_sha(_HELM_ORIGINAL_VALUES),
                content=_HELM_FORWARD_VALUES,
            )
        ],
        renderer=RendererKind.HELM_VALUES,
    )
    source = source_plan.source.model_copy(
        update={
            "renderer_config": {
                "chart": {"name": None, "repo": "https://github.com/acme/gitops.git"},
                "release_name": "payments",
                "release_namespace": "payments",
                "values_files": [_HELM_VALUES_PATH],
                "values_mapping": {
                    "file_path": _HELM_VALUES_PATH,
                    "container": "api",
                    "path": ["app", "resources"],
                },
            }
        }
    )
    renderer = _FakeHelmValuesRollbackRenderer()
    output_digest = renderer.render({_HELM_VALUES_PATH: _HELM_FORWARD_VALUES}, source).output_digest
    file_plan = source_plan.file_plan
    assert file_plan is not None
    file_plan = file_plan.model_copy(
        update={
            "validation": file_plan.validation.model_copy(update={"output_digest": output_digest}),
        }
    )
    baseline = (
        PreApplyWorkloadBaseline(
            workload_uid="uid-api",
            generation=7,
            observed_generation=7,
            container_requests={"api": ContainerRequestBaseline(cpu_millicores=100, memory_bytes=0)},
        )
        if include_baseline
        else None
    )
    plan = source_plan.model_copy(
        update={
            "source": source,
            "file_plan": file_plan,
            "pre_apply_baseline": baseline,
        }
    )
    return plan, {_HELM_VALUES_PATH: _HELM_ORIGINAL_VALUES}, {_HELM_VALUES_PATH: _HELM_FORWARD_VALUES}


def test_helm_values_inverse_uses_original_explicit_mapping_and_preapply_baseline() -> None:
    plan, original, current = _helm_values_rollback_plan()
    renderer = _FakeHelmValuesRollbackRenderer()

    inverse = build_inverse_plan(
        plan,
        original,
        current,
        CURRENT_BASE,
        renderer=renderer,
    )

    file_plan = plan.file_plan
    assert file_plan is not None
    assert [(edit.path, edit.operation, edit.content) for edit in inverse.files] == [
        (_HELM_VALUES_PATH, "update", _HELM_BASELINE_VALUES),
    ]
    assert inverse.files[0].original_sha == _blob_sha(_HELM_FORWARD_VALUES, revision=CURRENT_BASE)
    assert inverse.rendered_before_digest == file_plan.validation.output_digest
    assert renderer.mappings == [plan.source.renderer_config["values_mapping"]]


def test_helm_values_inverse_fails_closed_when_original_baseline_is_missing() -> None:
    plan, original, current = _helm_values_rollback_plan(include_baseline=False)

    with pytest.raises(RollbackConflictError, match="baseline"):
        build_inverse_plan(
            plan,
            original,
            current,
            CURRENT_BASE,
            renderer=_FakeHelmValuesRollbackRenderer(),
        )


def test_helm_values_inverse_fails_closed_when_unrelated_rendered_output_changed() -> None:
    plan, original, current = _helm_values_rollback_plan()

    with pytest.raises(RollbackConflictError, match="forward"):
        build_inverse_plan(
            plan,
            original,
            current,
            CURRENT_BASE,
            renderer=_FakeHelmValuesRollbackRenderer(unrelated_output=True),
        )


def test_helm_values_inverse_requires_current_values_to_match_exact_forward_output() -> None:
    plan, original, current = _helm_values_rollback_plan()
    current[_HELM_VALUES_PATH] = _HELM_FORWARD_VALUES.replace("300m", "250m")

    with pytest.raises(RollbackConflictError, match="forward plan output"):
        build_inverse_plan(
            plan,
            original,
            current,
            CURRENT_BASE,
            renderer=_FakeHelmValuesRollbackRenderer(),
        )


class _MismatchedDigestRenderer:
    renderer = RendererKind.RAW
    supports_exact_target_rollback = True

    def render(self, files: Mapping[str, str | bytes], source: WorkloadSource) -> RawRenderResult:
        return replace(RawManifestRollbackRenderer().render(files, source), output_digest=OUTPUT_DIGEST)


def test_renderer_output_digest_must_match_rendered_resources() -> None:
    plan = _plan(
        [
            FileEdit(
                path=MANIFEST_PATH,
                operation="update",
                original_sha=_blob_sha(BASE_CONTENT),
                content=FORWARD_CONTENT,
            )
        ]
    )

    with pytest.raises(RollbackValidationError, match="digest"):
        build_inverse_plan(
            plan,
            {MANIFEST_PATH: BASE_CONTENT},
            {MANIFEST_PATH: FORWARD_CONTENT},
            CURRENT_BASE,
            renderer=_MismatchedDigestRenderer(),
        )


class _FakeKustomizeRollbackRenderer:
    renderer = RendererKind.KUSTOMIZE
    supports_exact_target_rollback = True

    def render(self, files: Mapping[str, str | bytes], source: WorkloadSource) -> RawRenderResult:
        del source
        manifest = FORWARD_CONTENT if "apps/payments/patch.yaml" in files else BASE_CONTENT
        return replace(
            render_raw({MANIFEST_PATH: manifest}),
            renderer=RendererKind.KUSTOMIZE,
        )


class _KustomizeRollbackRunner:
    def __init__(self, *, ignore_inverse: bool = False, unrelated_change: bool = False) -> None:
        self.ignore_inverse = ignore_inverse
        self.unrelated_change = unrelated_change
        self.build_workspaces: list[Path] = []

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
        if tuple(argv[1:]) == ("version",):
            return SandboxResult(returncode=0, stdout=b"v5.6.0\n", stderr=b"")

        self.build_workspaces.append(workspace)
        patch_exists = (workspace / "apps/payments/patch.yaml").is_file()
        manifest = FORWARD_CONTENT if patch_exists or self.ignore_inverse else BASE_CONTENT
        if self.unrelated_change:
            mode = "new" if patch_exists else "old"
            manifest += f"""---
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
  namespace: payments
data:
  mode: {mode}
"""
        return SandboxResult(returncode=0, stdout=manifest.encode("utf-8"), stderr=b"")


class _RecordingKustomizeRenderer(KustomizeRenderer):
    def __init__(self, runner: _KustomizeRollbackRunner) -> None:
        super().__init__(expected_version="v5.6.0", runner=runner)
        self.revisions: list[str] = []

    def render(self, snapshot: RepositorySnapshot, overlay_path: str | Path) -> RawRenderResult:
        self.revisions.append(snapshot.metadata.commit_sha)
        return super().render(snapshot, overlay_path)


def _kustomize_inverse_input() -> tuple[Plan, dict[str, str], dict[str, str]]:
    original_kustomization = """\
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - deployment.yaml
"""
    forward_kustomization = (
        original_kustomization
        + """\
patches:
  - path: patch.yaml
    target:
      kind: Deployment
      name: api
      namespace: payments
"""
    )
    patch_path = "apps/payments/patch.yaml"
    patch = """\
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
            requests:
              cpu: 300m
"""
    plan = _plan(
        [
            FileEdit(
                path="apps/payments/kustomization.yaml",
                operation="update",
                original_sha=_blob_sha(original_kustomization),
                content=forward_kustomization,
            ),
            FileEdit(path=patch_path, operation="create", content=patch),
        ],
        renderer=RendererKind.KUSTOMIZE,
    )
    original_contents = {"apps/payments/kustomization.yaml": original_kustomization}
    current_files = {
        "apps/payments/kustomization.yaml": forward_kustomization,
        "apps/payments/deployment.yaml": BASE_CONTENT,
        patch_path: patch,
    }
    return plan, original_contents, current_files


def _archive_snapshot(revision: str) -> RepositorySnapshot:
    workspace_file = Path(__file__)
    content = workspace_file.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    metadata = SnapshotMetadata(
        provider_id="github-main",
        repo_url="https://github.com/acme/gitops.git",
        commit_sha=revision,
        snapshot_id="snapshot-1",
        archive_digest=None,
        tree_digest=f"sha256:{digest}",
        compressed_bytes=len(content),
        extracted_bytes=len(content),
        file_count=1,
        source_kind="provider_files",
        source_digest=f"sha256:{digest}",
    )
    return RepositorySnapshot(
        metadata=metadata,
        workspace=workspace_file.parent,
        files=(
            SnapshotFile(
                path=workspace_file.name,
                size_bytes=len(content),
                digest=f"sha256:{digest}",
            ),
        ),
        _workspace_owner=cast(Any, None),
    )


def test_kustomize_adapter_can_validate_inverse_using_full_candidate_snapshot() -> None:
    original_kustomization = "kind: Kustomization\nresources:\n- deployment.yaml\n"
    forward_kustomization = original_kustomization + "patches:\n- patch.yaml\n"
    patch_path = "apps/payments/patch.yaml"
    plan = _plan(
        [
            FileEdit(
                path="apps/payments/kustomization.yaml",
                operation="update",
                original_sha=_blob_sha(original_kustomization),
                content=forward_kustomization,
            ),
            FileEdit(path=patch_path, operation="create", content="patch: true\n"),
        ],
        renderer=RendererKind.KUSTOMIZE,
    )

    inverse = build_inverse_plan(
        plan,
        {"apps/payments/kustomization.yaml": original_kustomization},
        {
            "apps/payments/kustomization.yaml": forward_kustomization,
            patch_path: "patch: true\n",
        },
        CURRENT_BASE,
        renderer=_FakeKustomizeRollbackRenderer(),
    )

    assert [(edit.path, edit.operation) for edit in inverse.files] == [
        ("apps/payments/kustomization.yaml", "update"),
        (patch_path, "delete"),
    ]


def test_kustomize_rollback_renderer_renders_the_full_inverse_candidate_snapshot() -> None:
    plan, original_contents, current_files = _kustomize_inverse_input()
    runner = _KustomizeRollbackRunner()
    kustomize_renderer = _RecordingKustomizeRenderer(runner)
    renderer = KustomizeRollbackRenderer(kustomize_renderer)

    inverse = build_inverse_plan(
        plan,
        original_contents,
        current_files,
        CURRENT_BASE,
        renderer=renderer,
    )

    assert [(edit.path, edit.operation) for edit in inverse.files] == [
        ("apps/payments/kustomization.yaml", "update"),
        ("apps/payments/patch.yaml", "delete"),
    ]
    assert inverse.renderer is RendererKind.KUSTOMIZE
    assert inverse.rendered_before_digest != inverse.rendered_after_digest
    assert kustomize_renderer.revisions == [CURRENT_BASE, CURRENT_BASE]
    assert len(runner.build_workspaces) == 2
    assert runner.build_workspaces[0] != runner.build_workspaces[1]


def test_kustomize_rollback_fails_closed_when_inverse_render_does_not_change_target() -> None:
    plan, original_contents, current_files = _kustomize_inverse_input()
    renderer = KustomizeRollbackRenderer(
        KustomizeRenderer(expected_version="v5.6.0", runner=_KustomizeRollbackRunner(ignore_inverse=True))
    )

    with pytest.raises(RollbackValidationError, match="did not change"):
        build_inverse_plan(
            plan,
            original_contents,
            current_files,
            CURRENT_BASE,
            renderer=renderer,
        )


def test_kustomize_rollback_rejects_changes_to_unrelated_rendered_resources() -> None:
    plan, original_contents, current_files = _kustomize_inverse_input()
    renderer = KustomizeRollbackRenderer(
        KustomizeRenderer(expected_version="v5.6.0", runner=_KustomizeRollbackRunner(unrelated_change=True))
    )

    with pytest.raises(RollbackValidationError, match="unrelated"):
        build_inverse_plan(
            plan,
            original_contents,
            current_files,
            CURRENT_BASE,
            renderer=renderer,
        )


def test_repository_snapshot_revision_and_immutable_file_metadata_are_checked() -> None:
    plan = _plan(
        [FileEdit(path="apps/payments/patch.yaml", operation="create", content="patch: true\n")],
        renderer=RendererKind.KUSTOMIZE,
    )
    renderer = _FakeKustomizeRollbackRenderer()

    inverse = build_inverse_plan(plan, {}, _archive_snapshot(CURRENT_BASE), CURRENT_BASE, renderer=renderer)

    assert inverse.files == ()
    with pytest.raises(RollbackConflictError, match="revision"):
        build_inverse_plan(plan, {}, _archive_snapshot("c" * 40), CURRENT_BASE, renderer=renderer)


def test_inverse_identity_and_digest_include_current_base_revision() -> None:
    plan = _plan(
        [
            FileEdit(
                path=MANIFEST_PATH,
                operation="update",
                original_sha=_blob_sha(BASE_CONTENT),
                content=FORWARD_CONTENT,
            )
        ]
    )
    original = {MANIFEST_PATH: BASE_CONTENT}
    current = {MANIFEST_PATH: FORWARD_CONTENT}

    first = build_inverse_plan(plan, original, current, CURRENT_BASE)
    second = build_inverse_plan(plan, original, current, "c" * 40)

    assert first.plan_id != second.plan_id
    assert first.digest != second.digest
