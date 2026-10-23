"""Build fail-closed inverse file plans for validated GitOps plans."""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from greenkube.gitops.contracts import (
    ActionPlanV3,
    BindingStatus,
    ContainerRequestBaseline,
    ContainerResources,
    ControllerKind,
    FileEdit,
    FilePlan,
    Plan,
    PreApplyWorkloadBaseline,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    WorkloadSource,
)
from greenkube.gitops.planning.helm import (
    FluxHelmReleasePlanError,
    FluxHelmReleasePlanner,
    FluxHelmReleaseUnsupportedError,
    HelmValuesNeedsMappingError,
    HelmValuesPlanError,
    HelmValuesPlanner,
    HelmValuesUnsupportedError,
)
from greenkube.gitops.planning.raw_manifest import RawManifestPlanError, _containers, _pod_spec
from greenkube.gitops.renderers.kustomize import KustomizeRenderer
from greenkube.gitops.renderers.raw import RawRenderResult, RenderedResource, render_raw
from greenkube.gitops.snapshots import SnapshotLimits, snapshot_from_files
from greenkube.gitops.snapshots.models import RepositorySnapshot, SnapshotFile
from greenkube.utils.k8s_quantities import parse_cpu_quantity, parse_memory_quantity

_COMMIT_SHA_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_BLOB_SHA_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_RAW_MANIFEST_SUFFIXES = frozenset({".json", ".yaml", ".yml"})
_SUPPORTED_RENDERERS = frozenset(
    {RendererKind.RAW, RendererKind.KUSTOMIZE, RendererKind.HELM_VALUES, RendererKind.HELM_RELEASE}
)
ONE_TIME_HELM_VALUES_MAPPING_EVIDENCE_KEY = "_greenkube_plan_one_time_values_mapping"

_ResourceIdentity = tuple[str, str, str, str]


class RollbackPlanError(ValueError):
    """The persisted plan or repository state cannot be inverted safely."""


class RollbackConflictError(RollbackPlanError):
    """An affected path or immutable source no longer matches the plan."""


class RollbackValidationError(RollbackPlanError):
    """Rendering did not prove an exact target-only inverse."""


class UnsupportedRollbackError(RollbackPlanError):
    """The renderer or forward operation has no safe inverse implementation."""


class RollbackRenderer(Protocol):
    """Render a complete candidate repository for one exact workload source.

    Renderer adapters must include the whole source scope and its dependencies in
    each result. They may advertise exact rollback support only when they can
    render candidate file maps without omitting or mutating repository content.
    """

    renderer: RendererKind
    supports_exact_target_rollback: bool

    def render(
        self,
        files: Mapping[str, str | bytes],
        source: WorkloadSource,
    ) -> RawRenderResult: ...


@runtime_checkable
class _HelmValuesInversePlanner(Protocol):
    def plan_inverse(
        self,
        original_plan: Plan,
        files: Mapping[str, str | bytes],
        source: WorkloadSource,
        base_revision: str,
    ) -> list[FileEdit]: ...


class RawManifestRollbackRenderer:
    """Render the raw-manifest source scope using GreenKube's normalized renderer."""

    renderer = RendererKind.RAW
    supports_exact_target_rollback = True

    def render(
        self,
        files: Mapping[str, str | bytes],
        source: WorkloadSource,
    ) -> RawRenderResult:
        scope = source.path
        sources = {
            path: content
            for path, content in files.items()
            if (scope == "." or path == scope or path.startswith(f"{scope}/"))
            and PurePosixPath(path).suffix.lower() in _RAW_MANIFEST_SUFFIXES
        }
        return render_raw(sources)


class KustomizeRollbackRenderer:
    """Render complete candidate repository snapshots with an explicitly supplied Kustomize renderer."""

    renderer = RendererKind.KUSTOMIZE
    supports_exact_target_rollback = True

    def __init__(
        self,
        renderer: KustomizeRenderer,
        *,
        snapshot_limits: SnapshotLimits | None = None,
    ) -> None:
        self._renderer = renderer
        self._snapshot_limits = snapshot_limits or SnapshotLimits()

    def render(
        self,
        files: Mapping[str, str | bytes],
        source: WorkloadSource,
    ) -> RawRenderResult:
        """Render every candidate file at the verified current base revision."""
        revision = source.revision
        if not isinstance(revision, str) or not _COMMIT_SHA_PATTERN.fullmatch(revision):
            raise RollbackValidationError("Kustomize rollback requires the verified current base revision")
        snapshot = snapshot_from_files(
            files,
            provider_id=source.provider_id,
            repo_url=source.repo_url,
            commit_sha=revision,
            limits=self._snapshot_limits,
        )
        try:
            return self._renderer.render(snapshot, source.path)
        finally:
            snapshot.close()


class HelmValuesRollbackRenderer:
    """Render and plan inverses through the original Argo CD Helm values mapping."""

    renderer = RendererKind.HELM_VALUES
    supports_exact_target_rollback = True

    def __init__(
        self,
        planner: HelmValuesPlanner,
        action: ActionPlanV3,
        *,
        snapshot_limits: SnapshotLimits | None = None,
        workspace_parent: str | Path | None = None,
    ) -> None:
        self._planner = planner
        self._action = action
        self._snapshot_limits = snapshot_limits or SnapshotLimits()
        self._workspace_parent = workspace_parent

    def render(
        self,
        files: Mapping[str, str | bytes],
        source: WorkloadSource,
    ) -> RawRenderResult:
        """Render an exact candidate snapshot using only the original explicit mapping."""
        if source.renderer is not RendererKind.HELM_VALUES or source.controller_kind is not ControllerKind.ARGOCD:
            raise UnsupportedRollbackError("only Argo CD HELM_VALUES sources have a rollback renderer")
        revision = _validate_revision(source.revision or "")
        snapshot = self._candidate_snapshot(files, source, revision)
        try:
            return self._planner.render_source(
                self._action,
                source,
                snapshot,
                revision,
                mapping=source.renderer_config.get("values_mapping"),
            )
        except (HelmValuesNeedsMappingError, HelmValuesUnsupportedError) as exc:
            raise UnsupportedRollbackError(
                "Argo CD HELM_VALUES rollback requires the original unambiguous mapping and a supported local chart"
            ) from exc
        except HelmValuesPlanError as exc:
            raise RollbackValidationError("Helm values rollback render validation failed") from exc
        except Exception as exc:
            raise RollbackValidationError("Helm values rollback render failed closed") from exc
        finally:
            snapshot.close()

    def plan_inverse(
        self,
        original_plan: Plan,
        files: Mapping[str, str | bytes],
        source: WorkloadSource,
        base_revision: str,
    ) -> list[FileEdit]:
        """Create the target-only values edit from the persisted pre-apply baseline."""
        inverse_action = helm_values_inverse_action(original_plan)
        if not same_rollback_source(original_plan.source, source):
            raise RollbackConflictError("the original explicit Helm values mapping has changed")
        revision = _validate_revision(base_revision)
        snapshot = self._candidate_snapshot(files, source, revision)
        try:
            file_plan = self._planner.plan(
                inverse_action,
                source,
                snapshot,
                revision,
                mapping=original_plan.source.renderer_config.get("values_mapping"),
            )
        except (HelmValuesNeedsMappingError, HelmValuesUnsupportedError) as exc:
            raise UnsupportedRollbackError(
                "Argo CD HELM_VALUES rollback requires the original unambiguous mapping and a supported local chart"
            ) from exc
        except HelmValuesPlanError as exc:
            raise RollbackValidationError("Helm values inverse did not pass render validation") from exc
        except Exception as exc:
            raise RollbackValidationError("Helm values inverse planning failed closed") from exc
        finally:
            snapshot.close()

        if (
            not isinstance(file_plan, FilePlan)
            or file_plan.renderer is not RendererKind.HELM_VALUES
            or file_plan.base_revision.lower() != revision
            or not file_plan.validation.passed
        ):
            raise RollbackValidationError("Helm values planner returned an invalid inverse file plan")
        return list(file_plan.files)

    def _candidate_snapshot(
        self,
        files: Mapping[str, str | bytes],
        source: WorkloadSource,
        revision: str,
    ) -> RepositorySnapshot:
        try:
            return snapshot_from_files(
                files,
                provider_id=source.provider_id,
                repo_url=source.repo_url,
                commit_sha=revision,
                limits=self._snapshot_limits,
                workspace_parent=self._workspace_parent,
            )
        except Exception as exc:
            raise RollbackValidationError("Helm rollback candidate snapshot is invalid") from exc


class FluxHelmReleaseRollbackRenderer:
    """Render current and restored snapshots through the constrained Flux HelmRelease planner."""

    renderer = RendererKind.HELM_RELEASE
    supports_exact_target_rollback = True

    def __init__(
        self,
        planner: FluxHelmReleasePlanner,
        action: ActionPlanV3,
        manifest_path: str,
        *,
        snapshot_limits: SnapshotLimits | None = None,
        workspace_parent: str | Path | None = None,
    ) -> None:
        self._planner = planner
        self._action = action
        self._manifest_path = manifest_path
        self._snapshot_limits = snapshot_limits or SnapshotLimits()
        self._workspace_parent = workspace_parent

    def render(
        self,
        files: Mapping[str, str | bytes],
        source: WorkloadSource,
    ) -> RawRenderResult:
        """Render one candidate only at its immutable same-revision Flux chart source."""
        if (
            source.renderer is not RendererKind.HELM_RELEASE
            or source.controller_kind is not ControllerKind.FLUX_HELM_RELEASE
        ):
            raise UnsupportedRollbackError("only Flux HELM_RELEASE sources have this rollback renderer")
        revision = _validate_revision(source.revision or "")
        try:
            snapshot = snapshot_from_files(
                files,
                provider_id=source.provider_id,
                repo_url=source.repo_url,
                commit_sha=revision,
                limits=self._snapshot_limits,
                workspace_parent=self._workspace_parent,
            )
        except Exception as exc:
            raise RollbackValidationError("Flux HelmRelease rollback candidate snapshot is invalid") from exc
        try:
            return self._planner.render_source(
                self._action,
                source,
                snapshot,
                revision,
                expected_manifest_path=self._manifest_path,
            )
        except FluxHelmReleaseUnsupportedError as exc:
            raise UnsupportedRollbackError(
                "Flux HELM_RELEASE rollback requires a Git-owned HelmRelease, local same-revision chart, "
                "snapshot-resolvable values, and GreenKube-managed post-renderer patches"
            ) from exc
        except FluxHelmReleasePlanError as exc:
            raise RollbackValidationError("Flux HelmRelease rollback render validation failed") from exc
        except Exception as exc:
            raise RollbackValidationError("Flux HelmRelease rollback render failed closed") from exc
        finally:
            snapshot.close()


@dataclass(frozen=True, slots=True)
class InversePlan:
    """A deterministic, render-validated inverse file plan."""

    plan_id: str
    original_plan_id: str
    original_digest: str
    base_revision: str
    renderer: RendererKind
    target: ResourceTarget
    files: tuple[FileEdit, ...]
    diff: str
    rendered_diff: str
    rendered_before_digest: str
    rendered_after_digest: str
    warnings: tuple[str, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class _RenderedState:
    resources: Mapping[_ResourceIdentity, dict[str, Any]]
    output_digest: str
    rendered_json: str
    warnings: tuple[str, ...]


def build_inverse_plan(
    original_plan: Plan,
    original_contents: Mapping[str, str | bytes],
    current_snapshot: RepositorySnapshot | Mapping[str, str | bytes],
    current_base_revision: str,
    *,
    renderer: RollbackRenderer | None = None,
    current_source: WorkloadSource | None = None,
) -> InversePlan:
    """Create an inverse plan only for exact, render-validated forward output.

    The original content mapping contains the immutable pre-plan contents for
    updated paths. A current forward-created path may be absent, in which case
    its inverse is a no-op; if present, it must exactly match the planned output.
    Raw rendering is supported directly. Kustomize and Argo CD HELM_VALUES
    require their exact renderer adapters. Flux HELM_RELEASE is limited to its
    Git-owned manifest, local same-revision chart, snapshot-resolvable values,
    and managed post-renderer subset.
    """

    file_plan = _validate_original_plan(original_plan)
    base_revision = _validate_revision(current_base_revision)
    selected_renderer = _select_renderer(file_plan.renderer, renderer)
    before_files = _load_current_files(current_snapshot, base_revision)
    originals = _normalize_contents(original_contents, "original contents")
    original_inverse_files = _build_inverse_edits(file_plan.files, originals, before_files, base_revision)

    render_source = original_plan.source
    if current_source is not None:
        if not same_rollback_source(original_plan.source, current_source):
            raise RollbackConflictError("the resolved rollback source no longer matches the original plan")
        render_source = current_source
    elif file_plan.renderer is RendererKind.HELM_RELEASE:
        raise RollbackConflictError("Flux HELM_RELEASE rollback requires the currently resolved source binding")
    if (
        file_plan.renderer is RendererKind.HELM_VALUES
        and original_plan.source.evidence.get(ONE_TIME_HELM_VALUES_MAPPING_EVIDENCE_KEY) is True
    ):
        mapping = original_plan.source.renderer_config.get("values_mapping")
        if isinstance(mapping, Mapping):
            renderer_config = dict(render_source.renderer_config)
            renderer_config["values_mapping"] = mapping
            evidence = dict(render_source.evidence)
            evidence[ONE_TIME_HELM_VALUES_MAPPING_EVIDENCE_KEY] = True
            render_source = render_source.model_copy(update={"renderer_config": renderer_config, "evidence": evidence})
    if selected_renderer.renderer in {RendererKind.KUSTOMIZE, RendererKind.HELM_VALUES}:
        render_source = render_source.model_copy(update={"revision": base_revision, "source_revision": base_revision})
    elif selected_renderer.renderer is RendererKind.HELM_RELEASE:
        render_source = render_source.model_copy(update={"revision": base_revision})

    before = _render(selected_renderer, before_files, render_source)
    if (
        file_plan.renderer in {RendererKind.HELM_VALUES, RendererKind.HELM_RELEASE}
        and before.output_digest != file_plan.validation.output_digest
    ):
        renderer_name = "Flux HelmRelease" if file_plan.renderer is RendererKind.HELM_RELEASE else "Helm"
        raise RollbackConflictError(
            f"current {renderer_name} render no longer matches the persisted forward plan output"
        )

    inverse_files = original_inverse_files
    if file_plan.renderer is RendererKind.HELM_VALUES:
        if not isinstance(selected_renderer, _HelmValuesInversePlanner):
            raise UnsupportedRollbackError("HELM_VALUES rollback adapter cannot plan the baseline inverse")
        inverse_files = selected_renderer.plan_inverse(original_plan, before_files, render_source, base_revision)
        _validate_helm_inverse_edits(file_plan.files, inverse_files, before_files, base_revision)
    elif file_plan.renderer is RendererKind.HELM_RELEASE:
        _validate_flux_helm_release_inverse_edits(
            file_plan.files,
            inverse_files,
            before_files,
            base_revision,
            render_source,
        )

    after_files = dict(before_files)
    for edit in inverse_files:
        if edit.operation == "delete":
            after_files.pop(edit.path, None)
        else:
            after_files[edit.path] = edit.content or ""
    after = _render(selected_renderer, after_files, render_source)
    _validate_render_pair(
        before,
        after,
        original_plan.action.target,
        has_inverse_edits=bool(inverse_files),
    )
    if file_plan.renderer is RendererKind.HELM_RELEASE:
        _validate_flux_helm_release_rendered_baseline(original_plan, after.resources)

    diff = _file_diff(inverse_files, before_files)
    rendered_diff = _unified_diff(before.rendered_json, after.rendered_json, "rendered-before", "rendered-after")
    warnings = tuple(sorted(set(before.warnings + after.warnings)))
    plan_id = inverse_plan_id(original_plan.plan_id, file_plan.digest, base_revision)
    digest = _inverse_plan_digest(
        plan_id=plan_id,
        original_plan_id=original_plan.plan_id,
        original_digest=file_plan.digest,
        base_revision=base_revision,
        renderer=file_plan.renderer,
        target=original_plan.action.target,
        files=inverse_files,
        before=before,
        after=after,
        diff=diff,
        rendered_diff=rendered_diff,
        warnings=warnings,
    )
    return InversePlan(
        plan_id=plan_id,
        original_plan_id=original_plan.plan_id,
        original_digest=file_plan.digest,
        base_revision=base_revision,
        renderer=file_plan.renderer,
        target=original_plan.action.target,
        files=tuple(inverse_files),
        diff=diff,
        rendered_diff=rendered_diff,
        rendered_before_digest=before.output_digest,
        rendered_after_digest=after.output_digest,
        warnings=warnings,
        digest=digest,
    )


def _validate_original_plan(plan: Plan) -> FilePlan:
    if not isinstance(plan, Plan):
        raise RollbackConflictError("original plan must be a validated persisted Plan")
    file_plan = plan.file_plan
    if file_plan is None:
        raise UnsupportedRollbackError("original plan has no file plan to invert")
    if plan.digest is None or plan.digest != file_plan.digest:
        raise RollbackConflictError("original plan digest does not match its persisted file plan")
    if not file_plan.validation.passed:
        raise RollbackConflictError("original file plan did not pass render validation")
    if file_plan.renderer is not plan.source.renderer:
        raise RollbackConflictError("original plan source and file plan use different renderers")
    if plan.source.status is not BindingStatus.RESOLVED:
        raise RollbackConflictError("original plan source is not an exact resolved workload binding")
    if (
        plan.action.target.kind != plan.source.workload_kind
        or plan.action.target.namespace != plan.source.namespace
        or plan.action.target.name != plan.source.workload_name
    ):
        raise RollbackConflictError("original action target does not match its resolved workload source")
    if not all(isinstance(operation, SetContainerResources) for operation in plan.action.operations):
        raise UnsupportedRollbackError("only per-container resource operations have a supported inverse")
    if file_plan.renderer not in _SUPPORTED_RENDERERS:
        raise UnsupportedRollbackError(f"renderer {file_plan.renderer.value!r} does not support exact-target rollback")
    if file_plan.renderer is RendererKind.HELM_VALUES:
        mapping = plan.source.renderer_config.get("values_mapping")
        mapping_path = mapping.get("file_path") if isinstance(mapping, Mapping) else None
        if plan.source.controller_kind is not ControllerKind.ARGOCD:
            raise UnsupportedRollbackError("HELM_VALUES rollback is supported only for Argo CD")
        if (
            not isinstance(mapping_path, str)
            or len(file_plan.files) != 1
            or file_plan.files[0].operation != "update"
            or file_plan.files[0].path != mapping_path
        ):
            raise UnsupportedRollbackError(
                "HELM_VALUES rollback requires one original update at the persisted explicit mapping path"
            )
        helm_values_inverse_action(plan)
    elif file_plan.renderer is RendererKind.HELM_RELEASE:
        if plan.source.controller_kind is not ControllerKind.FLUX_HELM_RELEASE:
            raise UnsupportedRollbackError("HELM_RELEASE rollback is supported only for Flux HelmRelease sources")
        if len(file_plan.files) != 1 or file_plan.files[0].operation != "update" or file_plan.files[0].content is None:
            raise UnsupportedRollbackError(
                "HELM_RELEASE rollback may restore only its original Git-owned HelmRelease file"
            )
        manifest_source = plan.source.renderer_config.get("manifest_source")
        manifest_path = manifest_source.get("path") if isinstance(manifest_source, Mapping) else None
        if not isinstance(manifest_path, str) or manifest_path != plan.source.path:
            raise UnsupportedRollbackError("HELM_RELEASE rollback requires explicit Git ownership of its manifest")
        file_path = file_plan.files[0].path
        if not (
            plan.source.path == "." or file_path == plan.source.path or file_path.startswith(f"{plan.source.path}/")
        ):
            raise UnsupportedRollbackError("HELM_RELEASE rollback file is outside its Git-owned source path")
        if PurePosixPath(file_path).suffix.lower() not in {".yaml", ".yml", ".json"}:
            raise UnsupportedRollbackError("HELM_RELEASE rollback requires a YAML or JSON HelmRelease manifest")
        flux_helm_release_inverse_baseline(plan)
    for edit in file_plan.files:
        if edit.operation == "delete":
            raise UnsupportedRollbackError("forward delete file edits are not supported for rollback")
    return file_plan


def flux_helm_release_inverse_baseline(plan: Plan) -> PreApplyWorkloadBaseline:
    """Validate the persisted baseline and supported request-only Flux inverse scope."""
    if plan.source.renderer is not RendererKind.HELM_RELEASE:
        raise UnsupportedRollbackError("baseline-derived restoration is specific to Flux HELM_RELEASE rollback")
    if plan.source.controller_kind is not ControllerKind.FLUX_HELM_RELEASE:
        raise UnsupportedRollbackError("HELM_RELEASE rollback is supported only for Flux HelmRelease sources")
    baseline = plan.pre_apply_baseline
    if not isinstance(baseline, PreApplyWorkloadBaseline):
        raise RollbackConflictError("the original Flux HelmRelease plan has no persisted pre-apply baseline")
    if (
        not baseline.workload_uid.strip()
        or baseline.generation <= 0
        or baseline.observed_generation < baseline.generation
        or not baseline.container_requests
    ):
        raise RollbackConflictError("the persisted pre-apply workload baseline is invalid")
    operations: list[SetContainerResources] = []
    for operation in plan.action.operations:
        if not isinstance(operation, SetContainerResources):
            raise UnsupportedRollbackError("Flux HELM_RELEASE rollback requires per-container resource operations")
        operations.append(operation)
    if not operations:
        raise UnsupportedRollbackError("Flux HELM_RELEASE rollback requires per-container resource operations")
    containers: set[str] = set()
    for operation in operations:
        if (
            not operation.resources.requests
            or operation.resources.limits
            or set(operation.resources.requests) - {"cpu", "memory"}
        ):
            raise UnsupportedRollbackError(
                "Flux HELM_RELEASE rollback can restore only CPU and memory requests from the persisted baseline"
            )
        if operation.container in containers:
            raise UnsupportedRollbackError("Flux HELM_RELEASE rollback requires one operation per container")
        containers.add(operation.container)
    if not containers.issubset(baseline.container_requests):
        raise RollbackConflictError("the persisted pre-apply baseline omits an action container")
    return baseline


def _validate_flux_helm_release_inverse_edits(
    forward_edits: list[FileEdit],
    inverse_edits: list[FileEdit],
    current_contents: Mapping[str, str | bytes],
    current_base_revision: str,
    source: WorkloadSource,
) -> None:
    if (
        len(forward_edits) != 1
        or len(inverse_edits) != 1
        or inverse_edits[0].path != forward_edits[0].path
        or inverse_edits[0].operation != "update"
        or inverse_edits[0].content is None
    ):
        raise UnsupportedRollbackError("Flux HELM_RELEASE rollback may restore only its original manifest file")
    path = inverse_edits[0].path
    if not (source.path == "." or path == source.path or path.startswith(f"{source.path}/")):
        raise UnsupportedRollbackError("Flux HELM_RELEASE inverse file is outside its Git-owned source path")
    current = current_contents.get(path)
    if current is None:
        raise RollbackConflictError(f"{path}: current Git-owned HelmRelease manifest is missing")
    current_bytes = _content_bytes(current, path, "current snapshot")
    if _git_blob_sha(current_bytes, current_base_revision) != (inverse_edits[0].original_sha or "").lower():
        raise RollbackConflictError(f"{path}: Flux HelmRelease inverse is not based on the exact current manifest")


def _validate_flux_helm_release_rendered_baseline(
    plan: Plan,
    rendered_resources: Mapping[_ResourceIdentity, dict[str, Any]],
) -> None:
    baseline = flux_helm_release_inverse_baseline(plan)
    target_identity = _resource_identity(plan.action.target)
    target = rendered_resources.get(target_identity)
    if target is None:
        raise RollbackValidationError("Flux HelmRelease inverse render omitted the exact workload target")
    try:
        containers = _containers(_pod_spec(target, plan.action.target.kind))
        actual: dict[str, ContainerRequestBaseline] = {}
        for container in containers:
            name = container.get("name")
            if not isinstance(name, str) or not name.strip() or name != name.strip() or name in actual:
                raise RollbackValidationError("Flux HelmRelease inverse render has invalid container identities")
            resources = container.get("resources")
            if resources is None:
                resources = {}
            if not isinstance(resources, Mapping):
                raise RollbackValidationError("Flux HelmRelease inverse render has invalid container resources")
            requests = resources.get("requests")
            if requests is None:
                requests = {}
            if not isinstance(requests, Mapping):
                raise RollbackValidationError("Flux HelmRelease inverse render has invalid resource requests")
            actual[name] = ContainerRequestBaseline(
                cpu_millicores=_rendered_baseline_quantity(requests.get("cpu"), parse_cpu_quantity),
                memory_bytes=_rendered_baseline_quantity(requests.get("memory"), parse_memory_quantity),
            )
    except RawManifestPlanError as exc:
        raise RollbackValidationError("Flux HelmRelease inverse render has an invalid workload target") from exc
    except (TypeError, ValueError) as exc:
        raise RollbackValidationError("Flux HelmRelease inverse render has invalid resource quantities") from exc
    if actual != baseline.container_requests:
        raise RollbackValidationError(
            "Flux HelmRelease inverse render does not restore the persisted pre-apply per-container baseline"
        )


def _rendered_baseline_quantity(value: object, parser: Callable[[object], int | None]) -> int | None:
    if value is None:
        return None
    parsed = parser(value)
    if parsed is None:
        raise ValueError("rendered resource request is not a valid Kubernetes quantity")
    return parsed


def _validate_revision(value: str) -> str:
    if not isinstance(value, str) or not _COMMIT_SHA_PATTERN.fullmatch(value):
        raise RollbackConflictError("current base revision must be an immutable 40- or 64-character Git revision")
    return value.lower()


def _select_renderer(kind: RendererKind, renderer: RollbackRenderer | None) -> RollbackRenderer:
    if kind not in _SUPPORTED_RENDERERS:
        raise UnsupportedRollbackError(f"renderer {kind.value!r} does not support exact-target rollback")
    selected = renderer
    if selected is None:
        if kind is not RendererKind.RAW:
            renderer_name = (
                "HELM_VALUES"
                if kind is RendererKind.HELM_VALUES
                else "Flux HELM_RELEASE"
                if kind is RendererKind.HELM_RELEASE
                else kind.value.title()
            )
            raise UnsupportedRollbackError(
                f"{renderer_name} rollback requires an explicit exact-target renderer adapter"
            )
        selected = RawManifestRollbackRenderer()
    if getattr(selected, "renderer", None) is not kind:
        raise UnsupportedRollbackError("rollback renderer adapter does not match the original plan renderer")
    if getattr(selected, "supports_exact_target_rollback", False) is not True:
        raise UnsupportedRollbackError("rollback renderer adapter does not guarantee complete exact-target rendering")
    return selected


def _load_current_files(
    snapshot: RepositorySnapshot | Mapping[str, str | bytes],
    base_revision: str,
) -> dict[str, str | bytes]:
    if isinstance(snapshot, Mapping):
        return _normalize_contents(snapshot, "current snapshot")
    if not isinstance(snapshot, RepositorySnapshot):
        raise RollbackConflictError("current snapshot must be a repository snapshot or path-to-content mapping")
    if snapshot.metadata.commit_sha.lower() != base_revision:
        raise RollbackConflictError("current snapshot revision does not match the supplied base revision")

    try:
        root = snapshot.workspace.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RollbackConflictError("current snapshot workspace is unavailable") from exc
    if not root.is_dir():
        raise RollbackConflictError("current snapshot workspace is not a directory")

    contents: dict[str, str | bytes] = {}
    for snapshot_file in snapshot.files:
        if not isinstance(snapshot_file, SnapshotFile):
            raise RollbackConflictError("current snapshot contains invalid file metadata")
        path = _validate_repository_path(snapshot_file.path, "current snapshot")
        if path in contents:
            raise RollbackConflictError(f"{path}: current snapshot contains a duplicate file path")

        candidate = root
        for part in PurePosixPath(path).parts:
            candidate = candidate / part
            try:
                if candidate.is_symlink():
                    raise RollbackConflictError(f"{path}: current snapshot contains a symbolic link")
            except (OSError, RuntimeError) as exc:
                raise RollbackConflictError(f"{path}: current snapshot file is unavailable") from exc
        try:
            resolved = candidate.resolve(strict=True)
            metadata = candidate.stat()
            if not resolved.is_relative_to(root) or not stat.S_ISREG(metadata.st_mode):
                raise RollbackConflictError(f"{path}: current snapshot path is not a confined regular file")
            content = resolved.read_bytes()
        except (OSError, RuntimeError) as exc:
            raise RollbackConflictError(f"{path}: current snapshot file is unavailable") from exc

        actual_digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if snapshot_file.size_bytes != len(content) or snapshot_file.digest != actual_digest:
            raise RollbackConflictError(f"{path}: current snapshot content does not match its immutable metadata")
        contents[path] = content
    return _normalize_contents(contents, "current snapshot")


def _normalize_contents(
    contents: Mapping[str, str | bytes],
    description: str,
) -> dict[str, str | bytes]:
    if not isinstance(contents, Mapping):
        raise RollbackConflictError(f"{description} must be a path-to-content mapping")
    normalized: dict[str, str | bytes] = {}
    for raw_path, content in contents.items():
        path = _validate_repository_path(raw_path, description)
        if path in normalized:
            raise RollbackConflictError(f"{path}: {description} contains a duplicate path")
        if not isinstance(content, (str, bytes)):
            raise RollbackConflictError(f"{path}: {description} content must be text or bytes")
        normalized[path] = content
    paths = set(normalized)
    for path in paths:
        parts = path.split("/")
        for depth in range(1, len(parts)):
            parent = "/".join(parts[:depth])
            if parent in paths:
                raise RollbackConflictError(f"{parent}: {description} contains a file and its child path")
    return normalized


def _validate_repository_path(path: object, description: str) -> str:
    if (
        not isinstance(path, str)
        or not path
        or path != path.strip()
        or "\x00" in path
        or "\\" in path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise RollbackConflictError(f"{description} contains an unsafe repository path")
    if PurePosixPath(path).as_posix() != path:
        raise RollbackConflictError(f"{description} contains a non-canonical repository path")
    return path


def _build_inverse_edits(
    forward_edits: list[FileEdit],
    original_contents: Mapping[str, str | bytes],
    current_contents: Mapping[str, str | bytes],
    current_base_revision: str,
) -> list[FileEdit]:
    inverse_edits: list[FileEdit] = []
    for forward in sorted(forward_edits, key=lambda edit: edit.path):
        path = _validate_repository_path(forward.path, "forward file plan")
        if forward.operation == "delete":
            raise UnsupportedRollbackError("forward delete file edits are not supported for rollback")
        if forward.content is None:
            raise UnsupportedRollbackError(f"{path}: forward file edit has no reproducible output content")
        forward_bytes = _content_bytes(forward.content, path, "forward output")

        if forward.operation == "create":
            if path in original_contents:
                raise RollbackConflictError(f"{path}: forward create conflicts with supplied original contents")
            if path not in current_contents:
                continue
            current_bytes = _content_bytes(current_contents[path], path, "current snapshot")
            if current_bytes != forward_bytes:
                raise RollbackConflictError(f"{path}: created file no longer matches the forward plan output")
            inverse_edits.append(
                FileEdit(
                    path=path,
                    operation="delete",
                    original_sha=_git_blob_sha(current_bytes, current_base_revision),
                )
            )
            continue

        original_content = original_contents.get(path)
        if original_content is None:
            raise RollbackConflictError(f"{path}: immutable original source content is missing")
        original_bytes = _content_bytes(original_content, path, "original contents")
        _verify_original_blob(path, original_bytes, forward.original_sha)
        if path not in current_contents:
            raise RollbackConflictError(f"{path}: updated source path is missing from the current snapshot")
        current_bytes = _content_bytes(current_contents[path], path, "current snapshot")
        if current_bytes != forward_bytes:
            raise RollbackConflictError(f"{path}: current source no longer matches the forward plan output")
        original_text = _content_text(original_content, path, "original contents")
        inverse_edits.append(
            FileEdit(
                path=path,
                operation="update",
                original_sha=_git_blob_sha(current_bytes, current_base_revision),
                content=original_text,
            )
        )
    return inverse_edits


def _validate_helm_inverse_edits(
    forward_edits: list[FileEdit],
    inverse_edits: list[FileEdit],
    current_contents: Mapping[str, str | bytes],
    current_base_revision: str,
) -> None:
    if (
        not isinstance(inverse_edits, list)
        or len(forward_edits) != 1
        or len(inverse_edits) != 1
        or not isinstance(inverse_edits[0], FileEdit)
        or inverse_edits[0].path != forward_edits[0].path
        or inverse_edits[0].operation != "update"
        or inverse_edits[0].content is None
    ):
        raise UnsupportedRollbackError("HELM_VALUES rollback may update only its original mapped values file")
    path = inverse_edits[0].path
    current = current_contents.get(path)
    if current is None:
        raise RollbackConflictError(f"{path}: current mapped values file is missing")
    current_bytes = _content_bytes(current, path, "current snapshot")
    if _git_blob_sha(current_bytes, current_base_revision) != (inverse_edits[0].original_sha or "").lower():
        raise RollbackConflictError(f"{path}: inverse values edit is not based on the exact current file content")


def helm_values_inverse_action(plan: Plan) -> ActionPlanV3:
    """Build a planner-only inverse action from a persisted per-container baseline."""
    if plan.source.renderer is not RendererKind.HELM_VALUES:
        raise UnsupportedRollbackError("baseline-derived target resources are specific to HELM_VALUES rollback")
    if plan.source.controller_kind is not ControllerKind.ARGOCD:
        raise UnsupportedRollbackError("HELM_VALUES rollback is supported only for Argo CD")
    baseline = plan.pre_apply_baseline
    if not isinstance(baseline, PreApplyWorkloadBaseline):
        raise RollbackConflictError("the original plan has no persisted pre-apply workload baseline")
    operations = plan.action.operations
    if len(operations) != 1 or not isinstance(operations[0], SetContainerResources):
        raise UnsupportedRollbackError("HELM_VALUES rollback requires one explicitly mapped container operation")
    operation = operations[0]
    if not operation.resources.requests or operation.resources.limits:
        raise UnsupportedRollbackError(
            "HELM_VALUES rollback can restore only CPU and memory requests from the persisted baseline"
        )
    if set(operation.resources.requests) - {"cpu", "memory"}:
        raise UnsupportedRollbackError("the persisted baseline cannot restore this Helm resource request")
    container_baseline: ContainerRequestBaseline | None = baseline.container_requests.get(operation.container)
    if container_baseline is None:
        raise RollbackConflictError("the persisted pre-apply baseline omits the target container")

    requests: dict[str, str] = {}
    for resource in operation.resources.requests:
        value = container_baseline.cpu_millicores if resource == "cpu" else container_baseline.memory_bytes
        if value is None or isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RollbackConflictError(f"the persisted pre-apply baseline omits the target {resource} request")
        requests[resource] = f"{value}m" if resource == "cpu" else str(value)

    try:
        inverse_operation = SetContainerResources(
            container=operation.container,
            resources=ContainerResources(requests=requests),
        )
        return ActionPlanV3(
            action_id=plan.action.action_id,
            recommendation_ids=plan.action.recommendation_ids,
            target=plan.action.target,
            operations=[inverse_operation],
            constraints=plan.action.constraints,
            evidence_ref=plan.action.evidence_ref,
        )
    except ValidationError as exc:
        raise RollbackConflictError("the persisted pre-apply baseline cannot form a valid inverse action") from exc


def same_rollback_source(left: WorkloadSource, right: WorkloadSource) -> bool:
    """Compare source identity while allowing validated deployed revisions to advance."""
    left_config, right_config = _renderer_configs_for_comparison(left, right)
    same_renderer_config = left_config == right_config
    same_evidence = True
    if left.renderer is RendererKind.HELM_VALUES and right.renderer is RendererKind.HELM_VALUES:
        same_renderer_config = _same_helm_renderer_config(left_config, right_config)
    elif left.renderer is RendererKind.HELM_RELEASE and right.renderer is RendererKind.HELM_RELEASE:
        same_renderer_config = _same_flux_helm_release_renderer_config(
            left_config,
            right_config,
        )
        same_evidence = _same_flux_helm_release_evidence(left.evidence, right.evidence)
    return (
        left.id == right.id
        and left.cluster == right.cluster
        and left.namespace == right.namespace
        and left.workload_kind == right.workload_kind
        and left.workload_name == right.workload_name
        and left.provider_id == right.provider_id
        and left.repo_url == right.repo_url
        and left.ref == right.ref
        and left.path == right.path
        and left.controller_kind is right.controller_kind
        and left.controller_name == right.controller_name
        and left.controller_namespace == right.controller_namespace
        and left.renderer is right.renderer
        and same_renderer_config
        and same_evidence
    )


def same_resolved_renderer_config(expected: WorkloadSource, current: WorkloadSource) -> bool:
    """Compare binding renderer config without treating a plan-only map as a binding change."""
    expected_config, current_config = _renderer_configs_for_comparison(expected, current)
    return expected_config == current_config


def _renderer_configs_for_comparison(
    left: WorkloadSource,
    right: WorkloadSource,
) -> tuple[dict[str, Any], dict[str, Any]]:
    left_config = dict(left.renderer_config)
    right_config = dict(right.renderer_config)
    left_has_one_time_mapping = left.evidence.get(ONE_TIME_HELM_VALUES_MAPPING_EVIDENCE_KEY) is True
    right_has_one_time_mapping = right.evidence.get(ONE_TIME_HELM_VALUES_MAPPING_EVIDENCE_KEY) is True
    if left_has_one_time_mapping != right_has_one_time_mapping:
        if left_has_one_time_mapping:
            left_config.pop("values_mapping", None)
        else:
            right_config.pop("values_mapping", None)
    return left_config, right_config


def _same_helm_renderer_config(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    normalized_left = _normalize_helm_renderer_config(left)
    normalized_right = _normalize_helm_renderer_config(right)
    if normalized_left is None or normalized_right is None:
        return left == right
    return normalized_left == normalized_right


def _same_flux_helm_release_renderer_config(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    normalized_left = _normalize_flux_helm_release_renderer_config(left)
    normalized_right = _normalize_flux_helm_release_renderer_config(right)
    if normalized_left is None or normalized_right is None:
        return left == right
    return normalized_left == normalized_right


def _normalize_flux_helm_release_renderer_config(config: Mapping[str, Any]) -> dict[str, Any] | None:
    normalized = dict(config)
    normalized.pop("post_renderers", None)
    manifest_source = config.get("manifest_source")
    if isinstance(manifest_source, Mapping):
        source = dict(manifest_source)
        revision = source.get("revision")
        if revision is not None and (not isinstance(revision, str) or _revision_commit(revision) is None):
            return None
        source["revision"] = "__current_immutable_revision__"
        normalized["manifest_source"] = source
    return normalized


def _same_flux_helm_release_evidence(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    def normalize(evidence: Mapping[str, Any]) -> dict[str, Any] | None:
        normalized = dict(evidence)
        normalized.pop("artifact_revision", None)
        return normalized

    normalized_left = normalize(left)
    normalized_right = normalize(right)
    if normalized_left is None or normalized_right is None:
        return left == right
    return normalized_left == normalized_right


def _normalize_helm_renderer_config(config: Mapping[str, Any]) -> dict[str, Any] | None:
    normalized = dict(config)
    values_sources = config.get("values_sources")
    if not isinstance(values_sources, list):
        return normalized
    normalized_sources: list[dict[str, Any]] = []
    for entry in values_sources:
        if not isinstance(entry, Mapping):
            return None
        source = dict(entry)
        revision = source.get("source_revision")
        if revision is not None:
            if not isinstance(revision, str) or not revision.strip():
                return None
            source["source_revision"] = "__current_immutable_revision__"
        normalized_sources.append(source)
    normalized["values_sources"] = normalized_sources
    return normalized


def _verify_original_blob(path: str, content: bytes, original_sha: str | None) -> None:
    if original_sha is None or not _BLOB_SHA_PATTERN.fullmatch(original_sha):
        raise RollbackConflictError(f"{path}: immutable original blob SHA is missing or invalid")
    if _git_blob_sha(content, original_sha) != original_sha.lower():
        raise RollbackConflictError(f"{path}: immutable original content does not match its recorded blob SHA")


def _git_blob_sha(content: bytes, revision_or_sha: str) -> str:
    algorithm = hashlib.sha256 if len(revision_or_sha) == 64 else hashlib.sha1
    header = f"blob {len(content)}\0".encode("ascii")
    return algorithm(header + content).hexdigest()


def _revision_commit(value: str) -> str | None:
    if _COMMIT_SHA_PATTERN.fullmatch(value):
        return value.lower()
    match = re.search(r"(?:sha1|sha256):([0-9a-fA-F]{40}|[0-9a-fA-F]{64})$", value)
    return match.group(1).lower() if match is not None else None


def _content_bytes(content: str | bytes, path: str, description: str) -> bytes:
    if isinstance(content, bytes):
        return content
    try:
        return content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RollbackConflictError(f"{path}: {description} is not valid UTF-8 text") from exc


def _content_text(content: str | bytes, path: str, description: str) -> str:
    if isinstance(content, str):
        _content_bytes(content, path, description)
        return content
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RollbackConflictError(f"{path}: {description} is not valid UTF-8 text") from exc


def _render(
    renderer: RollbackRenderer,
    files: Mapping[str, str | bytes],
    source: WorkloadSource,
) -> _RenderedState:
    try:
        result = renderer.render(MappingProxyType(dict(files)), source)
    except RollbackPlanError:
        raise
    except Exception as exc:
        raise RollbackValidationError("rollback renderer rejected the repository snapshot") from exc
    if not isinstance(result, RawRenderResult) or result.renderer is not renderer.renderer:
        raise RollbackValidationError("rollback renderer returned an incompatible render result")
    if not isinstance(result.output_digest, str) or not _SHA256_PATTERN.fullmatch(result.output_digest):
        raise RollbackValidationError("rollback renderer returned an invalid output digest")
    if not isinstance(result.resources, tuple) or not isinstance(result.warnings, tuple):
        raise RollbackValidationError("rollback renderer returned invalid resource or warning collections")

    resources: dict[_ResourceIdentity, dict[str, Any]] = {}
    for resource in result.resources:
        if (
            not isinstance(resource, RenderedResource)
            or not isinstance(resource.target, ResourceTarget)
            or not isinstance(resource.manifest, dict)
        ):
            raise RollbackValidationError("rollback renderer returned an invalid resource")
        identity = _resource_identity(resource.target)
        if identity in resources:
            raise RollbackValidationError("rollback render contains duplicate resource identities")
        try:
            resources[identity] = copy.deepcopy(resource.manifest)
        except Exception as exc:
            raise RollbackValidationError("rollback renderer returned a non-copyable resource") from exc

    try:
        payload = _render_payload(resources)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        expected_digest = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
        rendered_json = json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise RollbackValidationError("rollback renderer returned non-deterministic resource data") from exc
    if expected_digest != result.output_digest:
        raise RollbackValidationError("rollback renderer output digest does not match its rendered resources")
    if not all(isinstance(warning, str) for warning in result.warnings):
        raise RollbackValidationError("rollback renderer returned invalid warnings")
    return _RenderedState(
        resources=MappingProxyType(resources),
        output_digest=result.output_digest,
        rendered_json=rendered_json,
        warnings=tuple(result.warnings),
    )


def _render_payload(resources: Mapping[_ResourceIdentity, dict[str, Any]]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for identity in sorted(resources):
        api_version, kind, namespace, name = identity
        payload.append(
            {
                "identity": {
                    "api_version": api_version,
                    "kind": kind,
                    "namespace": namespace,
                    "name": name,
                },
                "manifest": resources[identity],
            }
        )
    return payload


def _validate_render_pair(
    before: _RenderedState,
    after: _RenderedState,
    target: ResourceTarget,
    *,
    has_inverse_edits: bool,
) -> None:
    identity = _resource_identity(target)
    if identity not in before.resources or identity not in after.resources:
        raise RollbackValidationError("rollback render did not preserve the original exact resource target")
    if before.resources.keys() != after.resources.keys():
        raise RollbackValidationError("rollback render added or removed resources outside the original exact target")
    for resource_identity, manifest in before.resources.items():
        if resource_identity != identity and manifest != after.resources[resource_identity]:
            raise RollbackValidationError("rollback render changed an unrelated resource")
    target_changed = before.resources[identity] != after.resources[identity]
    if has_inverse_edits and not target_changed:
        raise RollbackValidationError("inverse file edits did not change the original exact resource target")
    if not has_inverse_edits and target_changed:
        raise RollbackValidationError("an empty inverse unexpectedly changed the rendered target")


def _resource_identity(target: ResourceTarget) -> _ResourceIdentity:
    return target.api_version, target.kind, target.namespace, target.name


def _file_diff(edits: list[FileEdit], before_files: Mapping[str, str | bytes]) -> str:
    chunks: list[str] = []
    for edit in edits:
        old_content = (
            _content_text(before_files[edit.path], edit.path, "current snapshot") if edit.path in before_files else ""
        )
        new_content = edit.content or ""
        from_file = "/dev/null" if edit.operation == "create" else f"a/{edit.path}"
        to_file = "/dev/null" if edit.operation == "delete" else f"b/{edit.path}"
        chunks.append(_unified_diff(old_content, new_content, from_file, to_file))
    return "".join(chunks)


def _unified_diff(before: str, after: str, from_file: str, to_file: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=from_file,
            tofile=to_file,
            lineterm="\n",
        )
    )


def inverse_plan_id(original_plan_id: str, original_digest: str, base_revision: str) -> str:
    """Return the deterministic identifier binding an inverse to its original plan and base."""
    payload = {
        "base_revision": base_revision,
        "original_digest": original_digest,
        "original_plan_id": original_plan_id,
        "version": 1,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"rbk-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:24]}"


def _inverse_plan_digest(
    *,
    plan_id: str,
    original_plan_id: str,
    original_digest: str,
    base_revision: str,
    renderer: RendererKind,
    target: ResourceTarget,
    files: list[FileEdit],
    before: _RenderedState,
    after: _RenderedState,
    diff: str,
    rendered_diff: str,
    warnings: tuple[str, ...],
) -> str:
    payload = {
        "base_revision": base_revision,
        "diff": diff,
        "files": [edit.model_dump(mode="json") for edit in files],
        "original_digest": original_digest,
        "original_plan_id": original_plan_id,
        "plan_id": plan_id,
        "rendered_after_digest": after.output_digest,
        "rendered_before_digest": before.output_digest,
        "rendered_diff": rendered_diff,
        "renderer": renderer.value,
        "target": {
            "api_version": target.api_version,
            "kind": target.kind,
            "namespace": target.namespace,
            "name": target.name,
        },
        "version": 1,
        "warnings": list(warnings),
    }
    try:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RollbackPlanError("inverse plan inputs are not deterministically JSON-serializable") from exc
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
