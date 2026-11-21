"""Shared, versioned contracts for GitOps discovery and automation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    StrictInt,
    StrictStr,
    ValidatorFunctionWrapHandler,
    WrapSerializer,
    WrapValidator,
    field_validator,
    model_serializer,
    model_validator,
)

from greenkube.gitops.sha import GIT_OBJECT_SHA, SHA256_DIGEST

_QUANTITY_PATTERN = re.compile(r"^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+|(?:n|u|m|k|K|M|G|T|P|E|Ki|Mi|Gi|Ti|Pi|Ei))?$")
_SCP_REPOSITORY_PATTERN = re.compile(r"^(?:[^/@:\s]+@)?(?P<host>[^:/\s]+):(?P<path>[^?#\s]+)$")
DEFAULT_BINDING_TTL = timedelta(minutes=30)
MAX_BINDING_TTL = timedelta(hours=24)
UNRESOLVED_PROVIDER_ID = "unresolved"


def configured_binding_ttl() -> timedelta:
    """Return the configured binding TTL, falling back to the documented default."""
    try:
        from greenkube.core.config import get_config

        configured = get_config().gitops_binding_ttl
    except Exception:
        return DEFAULT_BINDING_TTL
    if not isinstance(configured, timedelta) or configured <= timedelta(0) or configured > MAX_BINDING_TTL:
        return DEFAULT_BINDING_TTL
    return configured


class ContractModel(BaseModel):
    """Base class that rejects undeclared fields in cross-package contracts."""

    model_config = ConfigDict(extra="forbid")


class RendererKind(StrEnum):
    RAW = "raw"
    KUSTOMIZE = "kustomize"
    HELM_VALUES = "helm_values"
    HELM_RELEASE = "helm_release"


#: Renderers the planners, snapshot resolver and rollback can handle (one definition, roadmap M6).
PLANNABLE_RENDERERS: frozenset[RendererKind] = frozenset(
    {RendererKind.RAW, RendererKind.KUSTOMIZE, RendererKind.HELM_VALUES, RendererKind.HELM_RELEASE}
)


class ControllerKind(StrEnum):
    ARGOCD = "argocd"
    FLUX_KUSTOMIZATION = "flux_kustomization"
    FLUX_HELM_RELEASE = "flux_helm_release"
    MANUAL_RESOLUTION = "manual_resolution"


class BindingStatus(StrEnum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"
    BLOCKED = "blocked"
    STALE = "stale"


class ActionStatus(StrEnum):
    NONE = "none"
    GROUPED = "grouped"
    PLAN_READY = "plan_ready"
    PR_OPEN = "pr_open"
    MERGED = "merged"
    SYNC_PENDING = "sync_pending"
    APPLIED = "applied"
    CLOSED = "closed"
    FAILED = "failed"
    REVERTED = "reverted"


class PlanStatus(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    APPROVED = "approved"
    APPLYING = "applying"
    PR_OPEN = "pr_open"
    MERGED = "merged"
    SYNCED = "synced"
    APPLIED = "applied"
    SYNC_FAILED = "sync_failed"
    CLOSED = "closed"
    STALE = "stale"
    FAILED = "failed"
    NEEDS_MAPPING = "needs_mapping"
    AMBIGUOUS = "ambiguous"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    # Cancelled by a user (POST /automation/plans/{id}/cancel), reason ``cancelled_by_user`` (F-10).
    CANCELLED = "cancelled"


class OperationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AutomationErrorCode(StrEnum):
    PLAN_STALE = "plan_stale"
    PLAN_EXPIRED = "plan_expired"
    SOURCE_CHANGED = "source_changed"
    UNSUPPORTED_SOURCE = "unsupported_source"
    AMBIGUOUS_BINDING = "ambiguous_binding"
    NEEDS_CHART_MAPPING = "needs_chart_mapping"
    PROVIDER_NOT_READY = "provider_not_ready"
    PROVIDER_CAPABILITY_MISSING = "provider_capability_missing"
    AUTOMATION_BUDGET_EXHAUSTED = "automation_budget_exhausted"
    RENDER_VALIDATION_FAILED = "render_validation_failed"
    NO_EFFECTIVE_CHANGE = "no_effective_change"
    REQUIRES_CONTAINER_TARGET = "requires_container_target"
    REVISION_CONFLICT = "revision_conflict"
    UNSAFE_PATH = "unsafe_path"
    POLICY_EVIDENCE_MISSING = "policy_evidence_missing"
    POLICY_RISK_EXCEEDED = "policy_risk_exceeded"
    POLICY_CONFIDENCE_TOO_LOW = "policy_confidence_too_low"
    CHANGE_FREEZE_ACTIVE = "change_freeze_active"
    DELIVERY_PAUSED = "delivery_paused"
    WORKLOAD_PR_LIMIT_EXCEEDED = "workload_pr_limit_exceeded"
    WORKLOAD_COOLDOWN_ACTIVE = "workload_cooldown_active"
    WORKLOAD_PLAN_ALREADY_EXISTS = "workload_plan_already_exists"
    ROLLOUT_IN_PROGRESS = "rollout_in_progress"
    CONTROLLER_UNHEALTHY = "controller_unhealthy"
    POLICY_INTERACTION_CONFLICT = "policy_interaction_conflict"
    ROLLBACK_NOT_READY = "rollback_not_ready"
    ROLLBACK_UNSUPPORTED = "rollback_unsupported"
    ROLLBACK_CONFLICT = "rollback_conflict"
    DELIVERY_MODE_MISMATCH = "delivery_mode_mismatch"
    DELIVERY_NOT_VERIFIED = "delivery_not_verified"
    #: A requested recommendation is no longer active (dismissed, applied, expired, superseded).
    NOT_ACTIVE = "not_active"


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("value must not be blank")
    return value


def _repository_path(value: str, *, allow_root: bool) -> str:
    normalized = value.strip()
    if "\\" in normalized or normalized.startswith("/"):
        raise ValueError("path must be relative to the repository")
    path = PurePosixPath(normalized)
    if any(part == ".." for part in path.parts):
        raise ValueError("path must not escape the repository")
    canonical = path.as_posix()
    if not canonical or (canonical == "." and not allow_root):
        raise ValueError("path must identify a repository file or directory")
    return canonical


def _validate_digest(value: str) -> str:
    if not SHA256_DIGEST.fullmatch(value):
        raise ValueError("digest must be a sha256 digest")
    return value


class WorkloadSource(ContractModel):
    id: str = Field(min_length=1)
    cluster: str
    namespace: str = Field(min_length=1)
    workload_kind: str = Field(min_length=1)
    workload_name: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    repo_url: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    revision: str | None = None
    path: str
    controller_kind: ControllerKind
    controller_name: str
    controller_namespace: str
    controller_uid: StrictStr | None = None
    controller_generation: StrictInt | None = Field(default=None, ge=1)
    source_uid: StrictStr | None = None
    source_generation: StrictInt | None = Field(default=None, ge=1)
    source_revision: str | None = None
    renderer: RendererKind
    renderer_config: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0, le=1)
    status: BindingStatus
    evidence: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    reason: str | None = None

    @field_validator(
        "id",
        "namespace",
        "workload_kind",
        "workload_name",
        "provider_id",
        "repo_url",
        "ref",
    )
    @classmethod
    def validate_nonblank_fields(cls, value: str) -> str:
        return _nonblank(value)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _repository_path(value, allow_root=True)


def sanitize_repository_url(value: str) -> str:
    """Normalize a Git URL without userinfo or query credentials.

    The result is both the persisted form and the identity used to compare
    repositories (``canonical_repository_identity``).
    """
    if value == "unresolved":
        return value
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return "redacted"
    if parts.scheme.lower() in {"http", "https", "ssh", "git", "oci"} and host:
        normalized_host = f"[{host.lower()}]" if ":" in host else host.lower()
        netloc = f"{normalized_host}:{port}" if port is not None else normalized_host
        return f"{parts.scheme.lower()}://{netloc}{parts.path}"
    match = _SCP_REPOSITORY_PATTERN.fullmatch(value)
    if match is not None:
        return f"{match.group('host').lower()}:{match.group('path')}"
    return "redacted"


canonical_repository_identity = sanitize_repository_url


def public_repository_url(value: str) -> str:
    """Repository URL safe to return from the API: bounded, printable and credential-free."""
    if len(value) > 512 or any(ord(character) < 32 for character in value):
        return "redacted"
    return sanitize_repository_url(value)


def _safe_renderer_generation_config(value: Any) -> Any:
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            normalized_key = key.casefold().replace("-", "_")
            if normalized_key in {
                "values",
                "values_object",
                "helm_values",
                "inline_values",
                "parameters",
                "patch",
                "patches",
                "post_renderers",
                "repo",
                "repo_url",
                "repository",
                "url",
            } or any(
                sensitive in normalized_key
                for sensitive in ("password", "secret", "token", "credential", "authorization", "private_key")
            ):
                continue
            result[key] = _safe_renderer_generation_config(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_renderer_generation_config(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return type(value).__name__


def binding_candidate_generation_token(source: WorkloadSource) -> str:
    """Return an opaque fingerprint of the exact current source/controller identity."""
    identity = {
        "id": source.id,
        "cluster": source.cluster,
        "namespace": source.namespace,
        "workload_kind": source.workload_kind,
        "workload_name": source.workload_name,
        "controller_kind": source.controller_kind.value,
        "controller_name": source.controller_name,
        "controller_namespace": source.controller_namespace,
        "controller_uid": source.controller_uid,
        "controller_generation": source.controller_generation,
        "source_uid": source.source_uid,
        "source_generation": source.source_generation,
        "provider_id": source.provider_id,
        "repo_url": canonical_repository_identity(source.repo_url),
        "ref": source.ref,
        "revision": source.revision,
        "source_revision": source.source_revision,
        "path": source.path,
        "renderer": source.renderer.value,
        "renderer_config": _safe_renderer_generation_config(source.renderer_config),
        "status": source.status.value,
    }
    encoded = json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


class BindingResolution(ContractModel):
    status: BindingStatus
    binding: WorkloadSource | None = None
    candidates: list[WorkloadSource] = Field(default_factory=list)
    reason: str | None = None

    @model_validator(mode="after")
    def validate_resolution(self) -> BindingResolution:
        if self.status is BindingStatus.RESOLVED and self.binding is None:
            raise ValueError("resolved binding resolution requires a binding")
        if self.status is BindingStatus.AMBIGUOUS and self.binding is not None:
            raise ValueError("ambiguous binding resolution cannot select a binding")
        return self


class ResourceTarget(ContractModel):
    api_version: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    namespace: str
    name: str = Field(min_length=1)


class ContainerResources(ContractModel):
    requests: dict[str, StrictStr] = Field(default_factory=dict)
    limits: dict[str, StrictStr] = Field(default_factory=dict)

    @field_validator("requests", "limits")
    @classmethod
    def validate_quantities(cls, values: dict[str, str]) -> dict[str, str]:
        for resource, quantity in values.items():
            if not resource.strip() or not _QUANTITY_PATTERN.fullmatch(quantity):
                raise ValueError(f"invalid Kubernetes resource quantity for {resource!r}")
        return values


class TypedOperation(ContractModel):
    """Base of every typed action operation (``op`` discriminates the action kind).

    Built-in operations are listed in ``ActionOperation``; an action kind registered in
    ``greenkube.automation.actions`` adds its own operation model with
    ``register_operation_model`` without changing this module.
    """

    def conflict_key(self) -> str | None:
        """Identity of what the operation changes; two operations of one action may not share it."""
        return None


#: Pod-spec list a container lives in (H1): ``containers``, or ``initContainers`` for sidecars and init containers.
ContainerKind = Literal["container", "sidecar", "init"]
#: Container resources whose limit an operation may set or remove.
LimitResource = Literal["cpu", "memory"]


class SetContainerResources(TypedOperation):
    op: Literal["set_container_resources"] = "set_container_resources"
    container: str = Field(min_length=1)
    resources: ContainerResources
    #: Limits the operation removes (H1: only ``cpu`` is ever proposed by GreenKube).
    remove_limits: list[LimitResource] = Field(default_factory=list)
    #: Which pod-spec list holds the container (H1): ``containers``, or ``initContainers``
    #: for a native sidecar (``restartPolicy: Always``) or a classic init container.
    container_kind: ContainerKind = "container"

    @field_validator("container")
    @classmethod
    def validate_container(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("container must be a non-blank name without surrounding whitespace")
        return value

    @model_validator(mode="after")
    def validate_limit_removals(self) -> SetContainerResources:
        if len(set(self.remove_limits)) != len(self.remove_limits):
            raise ValueError("remove_limits must not repeat a resource")
        if set(self.remove_limits) & set(self.resources.limits):
            raise ValueError("a limit cannot be both set and removed")
        return self

    @model_serializer(mode="wrap")
    def _omit_v2_defaults(self, handler: SerializerFunctionWrapHandler) -> Any:
        # Operations without H1 fields serialize exactly as before (action ids, digests, golden plans).
        data = handler(self)
        if isinstance(data, dict):
            if not self.remove_limits:
                data.pop("remove_limits", None)
            if self.container_kind == "container":
                data.pop("container_kind", None)
        return data

    def conflict_key(self) -> str | None:
        return f"container {self.container!r}"


class DeleteResource(TypedOperation):
    op: Literal["delete_resource"] = "delete_resource"


class AddHorizontalPodAutoscaler(TypedOperation):
    op: Literal["add_hpa"] = "add_hpa"
    min_replicas: int = Field(ge=1)
    max_replicas: int = Field(ge=1)
    target_cpu_utilization: int | None = Field(default=None, ge=1, le=100)

    @model_validator(mode="after")
    def validate_replica_range(self) -> AddHorizontalPodAutoscaler:
        if self.min_replicas > self.max_replicas:
            raise ValueError("min_replicas must not exceed max_replicas")
        return self


_BUILTIN_OPERATION_MODELS: dict[str, type[TypedOperation]] = {
    "set_container_resources": SetContainerResources,
    "delete_resource": DeleteResource,
    "add_hpa": AddHorizontalPodAutoscaler,
}
#: Operation models of action kinds registered at runtime (H0), keyed by ``op``.
_EXTENSION_OPERATION_MODELS: dict[str, type[TypedOperation]] = {}


def register_operation_model(op: str, model: type[TypedOperation]) -> None:
    """Let ``ActionPlanV3`` parse and persist the typed operation of a registered action kind."""
    if not isinstance(op, str) or not op.strip():
        raise ValueError("operation names must be non-blank strings")
    if not isinstance(model, type) or not issubclass(model, TypedOperation):
        raise TypeError("operation models must subclass TypedOperation")
    builtin = _BUILTIN_OPERATION_MODELS.get(op)
    if builtin is not None:
        if builtin is not model:
            raise ValueError(f"operation {op!r} is a built-in operation")
        return
    existing = _EXTENSION_OPERATION_MODELS.get(op)
    if existing is not None and existing is not model:
        raise ValueError(f"operation {op!r} is already registered")
    _EXTENSION_OPERATION_MODELS[op] = model


def unregister_operation_model(op: str) -> None:
    """Remove a runtime operation model (tests only; built-ins cannot be removed)."""
    _EXTENSION_OPERATION_MODELS.pop(op, None)


def _validate_operation(value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
    if isinstance(value, TypedOperation) and type(value) in _EXTENSION_OPERATION_MODELS.values():
        return value
    if isinstance(value, Mapping):
        model = _EXTENSION_OPERATION_MODELS.get(str(value.get("op")))
        if model is not None:
            return model.model_validate(dict(value))
    return handler(value)


def _serialize_operation(value: Any, handler: SerializerFunctionWrapHandler, info: SerializationInfo) -> Any:
    if isinstance(value, TypedOperation) and type(value) in _EXTENSION_OPERATION_MODELS.values():
        return value.model_dump(mode=info.mode)
    return handler(value)


_BuiltinOperation = Annotated[
    SetContainerResources | DeleteResource | AddHorizontalPodAutoscaler,
    Field(discriminator="op"),
]

ActionOperation = Annotated[
    _BuiltinOperation,
    WrapValidator(_validate_operation),
    # The return type keeps the built-in discriminated union in the published
    # serialization schema (OpenAPI); registered kinds serialize as plain objects.
    WrapSerializer(_serialize_operation, return_type=_BuiltinOperation | dict[str, Any]),
]


class ActionPlanV3(ContractModel):
    version: Literal[3] = 3
    action_id: str = Field(min_length=1)
    recommendation_ids: list[int] = Field(min_length=1)
    target: ResourceTarget
    operations: list[ActionOperation] = Field(min_length=1)
    constraints: dict[str, Any] = Field(default_factory=dict)
    evidence_ref: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_action(self) -> ActionPlanV3:
        if len(set(self.recommendation_ids)) != len(self.recommendation_ids) or any(
            recommendation_id <= 0 for recommendation_id in self.recommendation_ids
        ):
            raise ValueError("recommendation_ids must be unique positive integers")
        keys: set[str] = set()
        for operation in self.operations:
            key = operation.conflict_key()
            if key is None:
                continue
            if key in keys:
                raise ValueError(f"each {key} may have only one resource operation")
            keys.add(key)
        return self


class FileEdit(ContractModel):
    path: str = Field(min_length=1)
    operation: Literal["create", "update", "delete"]
    original_sha: str | None = None
    content: str | None = None

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _repository_path(value, allow_root=False)

    @model_validator(mode="after")
    def validate_edit(self) -> FileEdit:
        if self.operation in ("create", "update") and self.content is None:
            raise ValueError("create and update file edits require content")
        if self.operation == "create" and self.original_sha is not None:
            raise ValueError("create file edits cannot have an original_sha")
        if self.operation in ("update", "delete") and not self.original_sha:
            raise ValueError(f"{self.operation} file edits require an original_sha")
        if self.operation == "delete" and self.content is not None:
            raise ValueError("delete file edits cannot have content")
        return self


def _not_recorded(value: Any) -> bool:
    return not value


class ValidationResult(ContractModel):
    target_changed: bool
    unrelated_changes: bool
    requests_within_limits: bool
    renderer: RendererKind
    output_digest: str
    warnings: list[str] = Field(default_factory=list)
    reason: str | None = None
    #: Every repository file the render read (Kustomize bases and patches, values files,
    #: chart files, raw manifests in scope) with its Git blob ID at the base revision.
    #: A moved branch head is accepted only while all of them are unchanged (D7).
    #: Omitted from the digest and the JSON form when empty (plans created before it).
    render_inputs: dict[str, str] = Field(default_factory=dict, exclude_if=_not_recorded)
    #: Directories where any added or changed file changes the render (a raw manifest
    #: directory, a chart directory), checked like ``render_inputs``.
    render_scopes: list[str] = Field(default_factory=list, exclude_if=_not_recorded)

    @field_validator("output_digest")
    @classmethod
    def validate_output_digest(cls, value: str) -> str:
        return _validate_digest(value)

    @field_validator("render_inputs")
    @classmethod
    def validate_render_inputs(cls, value: dict[str, str]) -> dict[str, str]:
        validated: dict[str, str] = {}
        for path, blob_sha in value.items():
            canonical = _repository_path(path, allow_root=False)
            if not isinstance(blob_sha, str) or GIT_OBJECT_SHA.fullmatch(blob_sha) is None:
                raise ValueError("render input blob IDs must be full Git object IDs")
            validated[canonical] = blob_sha.lower()
        return dict(sorted(validated.items()))

    @field_validator("render_scopes")
    @classmethod
    def validate_render_scopes(cls, value: list[str]) -> list[str]:
        return sorted({_repository_path(path, allow_root=True) for path in value})

    @property
    def passed(self) -> bool:
        return self.target_changed and not self.unrelated_changes and self.requests_within_limits


class FilePlan(ContractModel):
    plan_id: str = Field(min_length=1)
    files: list[FileEdit] = Field(min_length=1)
    diff: str
    rendered_diff: str
    base_revision: str = Field(min_length=1)
    source_revision: str | None = None
    renderer: RendererKind
    validation: ValidationResult
    warnings: list[str] = Field(default_factory=list)
    digest: str

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _validate_digest(value)

    @model_validator(mode="after")
    def validate_files(self) -> FilePlan:
        paths = [file_edit.path for file_edit in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("file edit paths must be unique")
        if self.validation.renderer is not self.renderer:
            raise ValueError("file plan renderer must match its validation renderer")
        return self


class FileSummaryEntry(ContractModel):
    """Content-free record of one planned file edit, kept after retention prunes file contents."""

    path: str = Field(min_length=1)
    operation: Literal["create", "update", "delete"]
    original_sha: str | None = None
    content_digest: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _repository_path(value, allow_root=False)

    @field_validator("content_digest")
    @classmethod
    def validate_content_digest(cls, value: str | None) -> str | None:
        return _validate_digest(value) if value is not None else None


class DiffStats(ContractModel):
    files_changed: int = Field(ge=0)
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)


class FilePlanSummary(ContractModel):
    """Compact, content-free projection of a FilePlan (paths, digests, diff stats).

    Retention keeps this summary when it prunes plan file contents so a terminal plan
    remains a valid, readable ``Plan``. ``files`` is empty only for plans pruned by
    releases that did not keep a summary.
    """

    plan_id: str = Field(min_length=1)
    files: list[FileSummaryEntry] = Field(default_factory=list)
    base_revision: str = Field(min_length=1)
    source_revision: str | None = None
    renderer: RendererKind
    validation: ValidationResult
    warnings: list[str] = Field(default_factory=list)
    digest: str
    diff_stats: DiffStats | None = None
    pruned_at: datetime | None = None

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _validate_digest(value)

    @model_validator(mode="after")
    def validate_summary(self) -> FilePlanSummary:
        paths = [entry.path for entry in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("file summary paths must be unique")
        if self.validation.renderer is not self.renderer:
            raise ValueError("file plan summary renderer must match its validation renderer")
        return self

    @classmethod
    def from_file_plan(cls, file_plan: FilePlan, *, pruned_at: datetime | None = None) -> FilePlanSummary:
        return cls(
            plan_id=file_plan.plan_id,
            files=[
                FileSummaryEntry(
                    path=edit.path,
                    operation=edit.operation,
                    original_sha=edit.original_sha,
                    content_digest=(
                        f"sha256:{hashlib.sha256(edit.content.encode('utf-8')).hexdigest()}"
                        if edit.content is not None
                        else None
                    ),
                    size_bytes=len(edit.content.encode("utf-8")) if edit.content is not None else None,
                )
                for edit in file_plan.files
            ],
            base_revision=file_plan.base_revision,
            source_revision=file_plan.source_revision,
            renderer=file_plan.renderer,
            validation=file_plan.validation,
            warnings=list(file_plan.warnings),
            digest=file_plan.digest,
            diff_stats=diff_stats(file_plan.diff, files_changed=len(file_plan.files)),
            pruned_at=pruned_at,
        )


def diff_stats(diff: str | None, *, files_changed: int | None = None) -> DiffStats:
    """Count unified-diff additions and deletions without keeping the diff text."""
    additions = deletions = 0
    changed_paths: set[str] = set()
    for line in (diff or "").splitlines():
        if line.startswith("+++ "):
            changed_paths.add(line[4:].strip())
        elif line.startswith("---"):
            continue
        elif line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return DiffStats(
        files_changed=files_changed if files_changed is not None else len(changed_paths),
        additions=additions,
        deletions=deletions,
    )


class ContainerRequestBaseline(ContractModel):
    cpu_millicores: StrictInt | None = Field(default=None, ge=0)
    memory_bytes: StrictInt | None = Field(default=None, ge=0)


class PreApplyWorkloadBaseline(ContractModel):
    workload_uid: StrictStr = Field(min_length=1)
    generation: StrictInt = Field(gt=0)
    observed_generation: StrictInt = Field(gt=0)
    container_requests: dict[str, ContainerRequestBaseline] = Field(min_length=1)

    @field_validator("workload_uid")
    @classmethod
    def validate_workload_uid(cls, value: str) -> str:
        return _nonblank(value)

    @field_validator("container_requests")
    @classmethod
    def validate_container_requests(
        cls,
        values: dict[str, ContainerRequestBaseline],
    ) -> dict[str, ContainerRequestBaseline]:
        if any(not name.strip() or name != name.strip() for name in values):
            raise ValueError("baseline container names must be non-blank and canonical")
        return values

    @model_validator(mode="after")
    def validate_observed_generation(self) -> PreApplyWorkloadBaseline:
        if self.observed_generation < self.generation:
            raise ValueError("baseline workload has not observed its current generation")
        return self


class Plan(ContractModel):
    plan_id: str = Field(min_length=1)
    recommendation_ids: list[int] = Field(min_length=1)
    source: WorkloadSource
    action: ActionPlanV3
    policy_result: dict[str, Any] = Field(default_factory=dict)
    file_plan: FilePlan | None = None
    # Set instead of ``file_plan`` once retention pruned the file contents (D2).
    file_plan_summary: FilePlanSummary | None = None
    status: PlanStatus = PlanStatus.DRAFT
    digest: str | None = None
    change_set_id: str | None = None
    rollback_of_plan_id: str | None = None
    pre_apply_baseline: PreApplyWorkloadBaseline | None = None
    # Branch head the worker committed on when it differs from the planned base revision (D7).
    committed_on_revision: str | None = Field(default=None, min_length=1)
    # How the approved plan is delivered (F4): GreenKube opens the PR ("direct"), or an
    # operator ("patch") or a CI job ("ci") applies the patch and reports the PR back.
    # None for plans not approved yet, and for plans approved before delivery modes.
    delivery_mode: Literal["direct", "patch", "ci"] | None = None
    # Reason code of the last status change (``cancelled_by_user``, ``plan_expired``, ...);
    # None for plans whose status has not changed since 0220 (F-10).
    status_reason: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        return _validate_digest(value) if value is not None else None

    @property
    def file_summary(self) -> FilePlanSummary | None:
        """Content-free file summary, available before and after retention pruning."""
        if self.file_plan is not None:
            return FilePlanSummary.from_file_plan(self.file_plan)
        return self.file_plan_summary

    @property
    def base_revision(self) -> str | None:
        if self.file_plan is not None:
            return self.file_plan.base_revision
        return self.file_plan_summary.base_revision if self.file_plan_summary is not None else None

    @model_validator(mode="after")
    def validate_plan(self) -> Plan:
        if len(set(self.recommendation_ids)) != len(self.recommendation_ids):
            raise ValueError("plan recommendation_ids must be unique")
        if set(self.recommendation_ids) != set(self.action.recommendation_ids):
            raise ValueError("plan and action recommendation_ids must match")
        if self.rollback_of_plan_id is not None:
            if not self.rollback_of_plan_id.strip():
                raise ValueError("rollback_of_plan_id must not be blank")
            if self.rollback_of_plan_id == self.plan_id:
                raise ValueError("a plan cannot roll back itself")
            if (self.file_plan is None and self.file_plan_summary is None) or self.digest is None:
                raise ValueError("rollback plans require a persisted file plan and digest")
        if self.file_plan is not None and self.file_plan.plan_id != self.plan_id:
            raise ValueError("file plan plan_id must match its plan")
        if self.file_plan_summary is not None:
            if self.file_plan is not None:
                raise ValueError("a plan carries either its file plan or a pruned file plan summary, not both")
            if self.file_plan_summary.plan_id != self.plan_id:
                raise ValueError("file plan summary plan_id must match its plan")
        if self.status in {
            PlanStatus.VALIDATED,
            PlanStatus.APPROVED,
            PlanStatus.APPLYING,
            PlanStatus.PR_OPEN,
            PlanStatus.MERGED,
            PlanStatus.SYNCED,
            PlanStatus.APPLIED,
            PlanStatus.SYNC_FAILED,
        }:
            validated = self.file_plan if self.file_plan is not None else self.file_plan_summary
            if validated is None or not validated.validation.passed:
                raise ValueError("validated plan status requires a successful render validation")
            if self.digest != validated.digest:
                raise ValueError("validated plan digest must match the file plan digest")
        return self


class Operation(ContractModel):
    operation_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    status: OperationStatus = OperationStatus.QUEUED
    attempts: int = Field(default=0, ge=0)
    available_at: datetime | None = None
    locked_at: datetime | None = None
    worker_id: str | None = None
    last_error: str | None = None
    provider_id: str | None = None
    commit_sha: str | None = None
    pull_request_number: int | None = Field(default=None, gt=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime | None = None


class PullRequestEvent(ContractModel):
    provider_id: str = Field(min_length=1)
    delivery_id: str = Field(min_length=1)
    repo: str = Field(min_length=1)
    number: int = Field(gt=0)
    action: Literal["opened", "synchronized", "closed", "reopened"]
    merged: bool = False
    merge_commit_sha: str | None = None
    head_sha: str | None = None
    base_sha: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
