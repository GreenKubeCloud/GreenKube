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

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator

_QUANTITY_PATTERN = re.compile(r"^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+|(?:n|u|m|k|K|M|G|T|P|E|Ki|Mi|Gi|Ti|Pi|Ei))?$")
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
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


class ControllerKind(StrEnum):
    ARGOCD = "argocd"
    FLUX_KUSTOMIZATION = "flux_kustomization"
    FLUX_HELM_RELEASE = "flux_helm_release"
    EXPLICIT_REPOSITORY = "explicit_repository"
    ANNOTATION = "annotation"
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
    REQUIRES_CONTAINER_TARGET = "requires_container_target"
    REVISION_CONFLICT = "revision_conflict"
    UNSAFE_PATH = "unsafe_path"
    POLICY_EVIDENCE_MISSING = "policy_evidence_missing"
    POLICY_RISK_EXCEEDED = "policy_risk_exceeded"
    POLICY_CONFIDENCE_TOO_LOW = "policy_confidence_too_low"
    CHANGE_FREEZE_ACTIVE = "change_freeze_active"
    WORKLOAD_PR_LIMIT_EXCEEDED = "workload_pr_limit_exceeded"
    WORKLOAD_COOLDOWN_ACTIVE = "workload_cooldown_active"
    WORKLOAD_PLAN_ALREADY_EXISTS = "workload_plan_already_exists"
    ROLLOUT_IN_PROGRESS = "rollout_in_progress"
    CONTROLLER_UNHEALTHY = "controller_unhealthy"
    POLICY_INTERACTION_CONFLICT = "policy_interaction_conflict"
    ROLLBACK_NOT_READY = "rollback_not_ready"
    ROLLBACK_UNSUPPORTED = "rollback_unsupported"
    ROLLBACK_CONFLICT = "rollback_conflict"


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
    if not _SHA256_PATTERN.fullmatch(value):
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


def canonical_repository_identity(value: str) -> str:
    """Normalize a Git URL for identity without retaining userinfo or query credentials."""
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
        normalized_netloc = f"{normalized_host}:{port}" if port is not None else normalized_host
        return f"{parts.scheme.lower()}://{normalized_netloc}{parts.path}"
    match = _SCP_REPOSITORY_PATTERN.fullmatch(value)
    if match is not None:
        return f"{match.group('host').lower()}:{match.group('path')}"
    return "redacted"


def sanitize_repository_url(value: str) -> str:
    """Remove repository URL userinfo and query credentials before persistence."""
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


class SetContainerResources(ContractModel):
    op: Literal["set_container_resources"] = "set_container_resources"
    container: str = Field(min_length=1)
    resources: ContainerResources

    @field_validator("container")
    @classmethod
    def validate_container(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("container must be a non-blank name without surrounding whitespace")
        return value


class DeleteResource(ContractModel):
    op: Literal["delete_resource"] = "delete_resource"


class AddHorizontalPodAutoscaler(ContractModel):
    op: Literal["add_hpa"] = "add_hpa"
    min_replicas: int = Field(ge=1)
    max_replicas: int = Field(ge=1)
    target_cpu_utilization: int | None = Field(default=None, ge=1, le=100)

    @model_validator(mode="after")
    def validate_replica_range(self) -> AddHorizontalPodAutoscaler:
        if self.min_replicas > self.max_replicas:
            raise ValueError("min_replicas must not exceed max_replicas")
        return self


ActionOperation = Annotated[
    SetContainerResources | DeleteResource | AddHorizontalPodAutoscaler,
    Field(discriminator="op"),
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
        containers = [
            operation.container for operation in self.operations if isinstance(operation, SetContainerResources)
        ]
        if len(set(containers)) != len(containers):
            raise ValueError("each container may have only one resource operation")
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


class ValidationResult(ContractModel):
    target_changed: bool
    unrelated_changes: bool
    requests_within_limits: bool
    renderer: RendererKind
    output_digest: str
    warnings: list[str] = Field(default_factory=list)
    reason: str | None = None

    @field_validator("output_digest")
    @classmethod
    def validate_output_digest(cls, value: str) -> str:
        return _validate_digest(value)

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
    status: PlanStatus = PlanStatus.DRAFT
    digest: str | None = None
    change_set_id: str | None = None
    rollback_of_plan_id: str | None = None
    pre_apply_baseline: PreApplyWorkloadBaseline | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        return _validate_digest(value) if value is not None else None

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
            if self.file_plan is None or self.digest is None:
                raise ValueError("rollback plans require a persisted file plan and digest")
        if self.file_plan is not None and self.file_plan.plan_id != self.plan_id:
            raise ValueError("file plan plan_id must match its plan")
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
            if self.file_plan is None or not self.file_plan.validation.passed:
                raise ValueError("validated plan status requires a successful render validation")
            if self.digest != self.file_plan.digest:
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
