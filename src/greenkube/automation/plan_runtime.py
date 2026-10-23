"""Production composition for the GitOps plan API and durable worker."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from pydantic import ValidationError

from greenkube.automation.git.base import (
    DEFAULT_AUTOMATION_PULL_REQUEST_OPTIONS,
    GitProviderArchiveLimitError,
    GitProviderCapability,
    GitProviderCapabilityError,
    GitProviderError,
    GitRepository,
    PullRequestOptions,
    parse_repo_url,
)
from greenkube.automation.git.credentials import (
    KubernetesSecretResolver,
    SecretReference,
    SecretResolverError,
)
from greenkube.automation.git.k8s_secret_resolver import (
    DEFAULT_PROVIDER_SECRET_MOUNT,
    MountedSecretFileResolver,
)
from greenkube.automation.git.registry import GitProviderRegistry
from greenkube.automation.plan_worker import (
    PlanGitProvider,
    PlanWorker,
    RegistryPlanProviderResolver,
)
from greenkube.automation.plans import (
    AutomationPlanError,
    AutomationPlanService,
    GitBlobFile,
    PlanOperationEnqueuer,
    PlanRevisionResolver,
)
from greenkube.automation.policy import (
    AutomationPolicyConfig,
    AutomationPolicyEvaluator,
    AutomationPolicyResult,
    PolicyPlanRepository,
)
from greenkube.automation.rollback import (
    ONE_TIME_HELM_VALUES_MAPPING_EVIDENCE_KEY,
    FluxHelmReleaseRollbackRenderer,
    HelmValuesRollbackRenderer,
    RollbackConflictError,
    RollbackPlanError,
    RollbackRenderer,
    RollbackValidationError,
    UnsupportedRollbackError,
    build_inverse_plan,
    flux_helm_release_inverse_baseline,
    helm_values_inverse_action,
    inverse_plan_id,
    same_resolved_renderer_config,
    same_rollback_source,
)
from greenkube.core.config import get_config
from greenkube.core.optimization.applied_detector import K8sWorkloadReader
from greenkube.gitops.contracts import (
    UNRESOLVED_PROVIDER_ID,
    ActionPlanV3,
    ActionStatus,
    AutomationErrorCode,
    BindingStatus,
    ControllerKind,
    Operation,
    OperationStatus,
    Plan,
    PlanStatus,
    RendererKind,
    SetContainerResources,
    WorkloadSource,
)
from greenkube.gitops.discovery.common import safe_repo_path
from greenkube.gitops.planning.helm import FluxHelmReleasePlanner, HelmValuesPlanner
from greenkube.gitops.planning.raw_manifest import RawManifestPlanner
from greenkube.gitops.snapshots import (
    RepositorySnapshot,
    SnapshotError,
    SnapshotLimitError,
    SnapshotLimits,
    extract_archive,
)
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus
from greenkube.storage.base_gitops_repository import (
    GitOpsProviderConfig,
    GitOpsRepository,
    WorkloadBinding,
)
from greenkube.storage.base_plan_operation_repository import PlanOperationRepository
from greenkube.storage.base_pull_request_repository import PullRequestRepository
from greenkube.storage.base_repository import RecommendationRepository

_PROVIDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SUPPORTED_PROVIDER_TYPES = frozenset({"github", "gitlab", "gitea"})
_COMMIT_SHA = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_MAX_PROVIDER_CONFIG_BYTES = 1024 * 1024
_MAX_CONFIGURED_PROVIDERS = 100


class PlanRuntimeConfigurationError(RuntimeError):
    """A safe, actionable error in plan runtime configuration."""


class PlanProvidersNotConfiguredError(PlanRuntimeConfigurationError):
    """No Git provider was configured; a plan queue can remain idle."""


@dataclass(frozen=True, slots=True)
class ConfiguredGitProvider:
    """Secret-free provider settings loaded from ``GITOPS_PROVIDERS_JSON``."""

    config: GitOpsProviderConfig
    repositories: tuple[str, ...]
    token_file: str


class GitRefRevisionReader(Protocol):
    """The optional immutable ref lookup contract required by plan operations."""

    async def get_ref_revision(self, repo: GitRepository, ref: str) -> str: ...


class PlanSnapshotResolver(Protocol):
    """Create a bounded source snapshot for one resolved workload binding."""

    async def create_snapshot(self, source: WorkloadSource) -> RepositorySnapshot: ...


class BoundedProviderArchiveReader(Protocol):
    """Retrieve a bounded archive for one immutable repository commit."""

    async def get_archive(self, repository: GitRepository, revision: str, *, max_bytes: int) -> bytes: ...


class GitOpsProviderStore(Protocol):
    """Provider configuration and allowlist operations used during bootstrap."""

    async def save_provider_config(self, provider: GitOpsProviderConfig) -> GitOpsProviderConfig: ...

    async def get_provider_config(self, provider_id: str) -> GitOpsProviderConfig | None: ...

    async def list_provider_configs(self, *, enabled_only: bool = False) -> list[GitOpsProviderConfig]: ...

    async def add_provider_allowlist(self, provider_id: str, repository_pattern: str) -> None: ...

    async def list_provider_allowlists(self, provider_id: str) -> list[str]: ...

    async def remove_provider_allowlist(self, provider_id: str, repository_pattern: str) -> bool: ...


class PlanRecommendationReader(Protocol):
    """Read persisted actionable recommendation records for API plan creation."""

    async def get_recommendation_by_id(self, rec_id: int) -> RecommendationRecord | None: ...


class PlanBindingReader(Protocol):
    """Read resolved workload bindings from server-side GitOps storage."""

    async def get_workload_binding(
        self,
        cluster: str,
        namespace: str,
        workload_kind: str,
        workload_name: str,
    ) -> WorkloadBinding | None: ...


class RegistryPlanRevisionResolver(PlanRevisionResolver):
    """Resolve a current ref through a repository-authorized Git provider."""

    def __init__(self, provider_resolver: RegistryPlanProviderResolver) -> None:
        self._provider_resolver = provider_resolver

    async def resolve_current_revision(self, source: WorkloadSource) -> str:
        target = await self._provider_resolver.resolve(source)
        try:
            reader = cast(GitRefRevisionReader, target.provider)
            try:
                return await reader.get_ref_revision(target.repository, source.ref)
            except AttributeError:
                raise GitProviderCapabilityError(
                    target.provider.name,
                    "get_ref_revision",
                    reason="The Git adapter must expose an immutable ref lookup.",
                ) from None
        finally:
            await _close_provider(target.provider)


class AuthorizedGitBlobResolver:
    """Resolve immutable Git blob data through the provider registry allowlist."""

    def __init__(self, provider_resolver: RegistryPlanProviderResolver) -> None:
        self._provider_resolver = provider_resolver

    async def get_blob_file(self, source: WorkloadSource, path: str, revision: str) -> GitBlobFile | None:
        target = await self._provider_resolver.resolve(source)
        try:
            git_file = await target.provider.get_file(target.repository, path, revision)
            if git_file is None:
                return None
            blob_sha = git_file.blob_sha
            if blob_sha is None and target.provider.name in {"github", "gitea"}:
                blob_sha = git_file.sha
            if blob_sha is None:
                raise AutomationPlanError(
                    AutomationErrorCode.PROVIDER_NOT_READY,
                    f"provider returned no verified Git blob SHA for {path!r} at the immutable base revision",
                )
            return GitBlobFile(path=git_file.path, content=git_file.content, blob_sha=blob_sha)
        finally:
            await _close_provider(target.provider)


class ProviderSourceSnapshotResolver:
    """Build bounded snapshots from authorized provider archives pinned to an immutable commit."""

    def __init__(
        self,
        provider_resolver: RegistryPlanProviderResolver,
        *,
        limits: SnapshotLimits | None = None,
    ) -> None:
        self._provider_resolver = provider_resolver
        self._limits = limits or SnapshotLimits()

    async def create_snapshot(self, source: WorkloadSource) -> RepositorySnapshot:
        if source.renderer not in {
            RendererKind.RAW,
            RendererKind.KUSTOMIZE,
            RendererKind.HELM_VALUES,
            RendererKind.HELM_RELEASE,
        }:
            raise AutomationPlanError(
                AutomationErrorCode.UNSUPPORTED_SOURCE,
                f"renderer {source.renderer.value!r} does not have a configured snapshot planner",
            )
        target = await self._provider_resolver.resolve(source)
        try:
            if target.repository.provider != target.provider.name:
                raise AutomationPlanError(
                    AutomationErrorCode.SOURCE_CHANGED,
                    "authorized provider repository identity does not match the resolved source",
                )
            try:
                owner, name = parse_repo_url(source.repo_url, provider=target.repository.provider)
            except GitProviderError:
                raise AutomationPlanError(
                    AutomationErrorCode.SOURCE_CHANGED,
                    "resolved source repository identity is invalid",
                ) from None
            if (owner, name) != (target.repository.owner, target.repository.name):
                raise AutomationPlanError(
                    AutomationErrorCode.SOURCE_CHANGED,
                    "authorized provider repository identity does not match the resolved source",
                )

            revision = await _current_revision(target.provider, target.repository, source.ref)
            if not isinstance(revision, str) or not _COMMIT_SHA.fullmatch(revision):
                raise AutomationPlanError(
                    AutomationErrorCode.PROVIDER_NOT_READY,
                    "the provider did not resolve the source ref to an immutable full Git commit SHA",
                )
            supports = getattr(target.provider, "supports", None)
            if not callable(supports) or not supports(GitProviderCapability.REPOSITORY_ARCHIVE):
                raise AutomationPlanError(
                    AutomationErrorCode.PROVIDER_NOT_READY,
                    "configured Git provider does not support bounded immutable-commit archives",
                )
            if not callable(getattr(target.provider, "get_archive", None)):
                raise AutomationPlanError(
                    AutomationErrorCode.PROVIDER_NOT_READY,
                    "configured Git provider does not implement bounded immutable-commit archives",
                )
            archive_provider = cast(BoundedProviderArchiveReader, target.provider)
            try:
                archive_data = await archive_provider.get_archive(
                    target.repository,
                    revision,
                    max_bytes=self._limits.max_compressed_bytes,
                )
            except GitProviderCapabilityError as exc:
                if exc.capability != GitProviderCapability.REPOSITORY_ARCHIVE.value:
                    raise
                raise AutomationPlanError(
                    AutomationErrorCode.PROVIDER_NOT_READY,
                    "configured Git provider does not support bounded immutable-commit archives",
                ) from exc
            except GitProviderArchiveLimitError as exc:
                raise PlanRuntimeConfigurationError(
                    f"Provider archive exceeds the configured {self._limits.max_compressed_bytes}-byte "
                    "compressed snapshot limit."
                ) from exc

            if not isinstance(archive_data, bytes):
                raise AutomationPlanError(
                    AutomationErrorCode.PROVIDER_NOT_READY,
                    "provider returned an invalid repository archive",
                )
            if len(archive_data) > self._limits.max_compressed_bytes:
                raise PlanRuntimeConfigurationError(
                    f"Provider archive exceeds the configured {self._limits.max_compressed_bytes}-byte "
                    "compressed snapshot limit."
                )
            try:
                return extract_archive(
                    archive_data,
                    provider_id=source.provider_id,
                    repo_url=source.repo_url,
                    commit_sha=revision,
                    limits=self._limits,
                    strip_top_level_directory=True,
                )
            except SnapshotLimitError as exc:
                raise PlanRuntimeConfigurationError(
                    "Provider archive exceeds the configured repository snapshot limits."
                ) from exc
            except SnapshotError as exc:
                raise AutomationPlanError(
                    AutomationErrorCode.PROVIDER_NOT_READY,
                    "provider returned an invalid or unsafe immutable repository archive",
                ) from exc
        finally:
            await _close_provider(target.provider)


class RuntimePlanService:
    """API-facing plan operations derived only from persisted recommendations and bindings."""

    def __init__(
        self,
        *,
        recommendation_repository: PlanRecommendationReader,
        binding_repository: PlanBindingReader,
        cluster: str,
        snapshot_resolver: PlanSnapshotResolver,
        plan_service: AutomationPlanService,
        worker: PlanWorker | None = None,
        rollback_renderer: RollbackRenderer | None = None,
        helm_values_planner: HelmValuesPlanner | None = None,
        snapshot_limits: SnapshotLimits | None = None,
        snapshot_workspace_parent: str | Path | None = None,
        policy_config: AutomationPolicyConfig | None = None,
        policy_repository: PolicyPlanRepository | None = None,
        policy_clock=None,
    ) -> None:
        self._recommendation_repository = recommendation_repository
        self._binding_repository = binding_repository
        self._cluster = cluster or "default"
        self._snapshot_resolver = snapshot_resolver
        self._plan_service = plan_service
        self._rollback_renderer = rollback_renderer
        configured_planners = getattr(plan_service, "planners", None)
        configured_helm_planner = (
            configured_planners.get(RendererKind.HELM_VALUES) if isinstance(configured_planners, Mapping) else None
        )
        configured_flux_planner = (
            configured_planners.get(RendererKind.HELM_RELEASE) if isinstance(configured_planners, Mapping) else None
        )
        self._helm_values_planner = helm_values_planner or (
            configured_helm_planner if isinstance(configured_helm_planner, HelmValuesPlanner) else None
        )
        self._flux_helm_release_planner = (
            configured_flux_planner if isinstance(configured_flux_planner, FluxHelmReleasePlanner) else None
        )
        self._snapshot_limits = snapshot_limits
        self._snapshot_workspace_parent = snapshot_workspace_parent
        self.worker = worker
        selected_policy_repository = policy_repository or getattr(plan_service, "repository", None)
        selected_policy_clock = policy_clock or getattr(plan_service, "clock", None)
        self._policy_evaluator = AutomationPolicyEvaluator(
            config=policy_config or AutomationPolicyConfig(),
            plan_repository=cast(PolicyPlanRepository | None, selected_policy_repository),
            clock=selected_policy_clock or (lambda: datetime.now(timezone.utc)),
        )

    async def create_plan(
        self,
        recommendation_ids: Sequence[int],
        *,
        change_set_id: str | None = None,
        actor: str = "system",
        values_mapping: Mapping[str, Any] | None = None,
    ) -> Plan:
        """Create a plan from persisted actionable recommendations and a resolved DB binding."""
        action, source = await self._resolve_request(recommendation_ids)
        policy_result = await self._evaluate_policy(action.recommendation_ids, source)
        self._ensure_policy_allowed(policy_result)
        if source.renderer not in {
            RendererKind.RAW,
            RendererKind.KUSTOMIZE,
            RendererKind.HELM_VALUES,
            RendererKind.HELM_RELEASE,
        }:
            raise AutomationPlanError(
                AutomationErrorCode.UNSUPPORTED_SOURCE,
                f"renderer {source.renderer.value!r} is not supported by the configured plan service",
            )
        if values_mapping is not None:
            source = _with_one_time_helm_values_mapping(action, source, values_mapping)

        try:
            snapshot = await self._snapshot_resolver.create_snapshot(source)
            try:
                try:
                    return await self._plan_service.create_plan(
                        action,
                        source,
                        snapshot,
                        policy_result=policy_result.as_dict(),
                        change_set_id=change_set_id,
                        actor=actor,
                    )
                except AutomationPlanError as exc:
                    if exc.code is not AutomationErrorCode.NEEDS_CHART_MAPPING or exc.mapping_options is not None:
                        raise
                    mapping_options = _helm_values_mapping_options(action, source, snapshot)
                    if mapping_options is None:
                        raise
                    raise AutomationPlanError(
                        exc.code,
                        exc.detail,
                        policy_result=exc.policy_result,
                        mapping_options=mapping_options,
                    ) from exc
            finally:
                snapshot.close()
        except PlanRuntimeConfigurationError as exc:
            raise AutomationPlanError(
                AutomationErrorCode.PROVIDER_NOT_READY,
                "the repository snapshot is unavailable or exceeds configured planning limits",
            ) from exc

    async def create_plan_for_recommendations(
        self,
        recommendation_ids: Sequence[int],
        *,
        change_set_id: str | None = None,
        actor: str = "system",
        values_mapping: Mapping[str, Any] | None = None,
    ) -> Plan:
        """Explicit alias for the source-safe plan creation entry point."""
        return await self.create_plan(
            recommendation_ids,
            change_set_id=change_set_id,
            actor=actor,
            values_mapping=values_mapping,
        )

    async def prepare_plan(
        self,
        recommendation_ids: Sequence[int],
        *,
        actor: str = "system",
        values_mapping: Mapping[str, Any] | None = None,
    ) -> Plan:
        """API protocol alias for source-safe preparation from persisted recommendation IDs."""
        return await self.create_plan(recommendation_ids, actor=actor, values_mapping=values_mapping)

    async def prune_retained_history(self, *, cutoff: datetime) -> dict[str, int]:
        """Prune expired rendered content and terminal outbox rows while retaining plan metadata."""
        return await self._plan_service.prune_retained_history(cutoff=cutoff)

    async def preview_plan(self, recommendation_ids: Sequence[int]) -> Plan:
        """Render an eligibility preview without persisting plans or changing recommendation state."""
        action, source = await self._resolve_request(recommendation_ids)
        policy_result = await self._evaluate_policy(action.recommendation_ids, source)
        self._ensure_policy_allowed(policy_result)
        if source.renderer not in {
            RendererKind.RAW,
            RendererKind.KUSTOMIZE,
            RendererKind.HELM_VALUES,
            RendererKind.HELM_RELEASE,
        }:
            raise AutomationPlanError(
                AutomationErrorCode.UNSUPPORTED_SOURCE,
                f"renderer {source.renderer.value!r} is not supported by the configured plan service",
            )

        try:
            snapshot = await self._snapshot_resolver.create_snapshot(source)
            try:
                return await self._plan_service.preview_plan(
                    action,
                    source,
                    snapshot,
                    policy_result=policy_result.as_dict(),
                )
            finally:
                snapshot.close()
        except PlanRuntimeConfigurationError as exc:
            raise AutomationPlanError(
                AutomationErrorCode.PROVIDER_NOT_READY,
                "the repository snapshot is unavailable or exceeds configured planning limits",
            ) from exc

    async def _resolve_request(self, recommendation_ids: Sequence[int]) -> tuple[ActionPlanV3, WorkloadSource]:
        if (
            not isinstance(recommendation_ids, (list, tuple))
            or not recommendation_ids
            or any(
                type(recommendation_id) is not int or recommendation_id <= 0 for recommendation_id in recommendation_ids
            )
            or len(set(recommendation_ids)) != len(recommendation_ids)
        ):
            raise AutomationPlanError(
                AutomationErrorCode.REQUIRES_CONTAINER_TARGET,
                "one or more valid persisted recommendation IDs are required",
            )

        requested_records: dict[int, RecommendationRecord] = {}
        for recommendation_id in recommendation_ids:
            record = await self._recommendation_repository.get_recommendation_by_id(recommendation_id)
            if record is None or record.id != recommendation_id:
                raise AutomationPlanError(
                    AutomationErrorCode.REQUIRES_CONTAINER_TARGET,
                    "one or more persisted recommendations were not found",
                )
            if record.status is not RecommendationStatus.ACTIVE or record.scope != "workload":
                raise AutomationPlanError(
                    AutomationErrorCode.REQUIRES_CONTAINER_TARGET,
                    "only active workload recommendations can be planned",
                )
            requested_records[recommendation_id] = record

        action = next(iter(requested_records.values())).action
        if (
            action is None
            or not action.recommendation_ids
            or not set(recommendation_ids).issubset(action.recommendation_ids)
        ):
            raise AutomationPlanError(
                AutomationErrorCode.REQUIRES_CONTAINER_TARGET,
                "recommendations do not share a persisted actionable plan",
            )
        action_data = action.model_dump(mode="json")
        records: list[RecommendationRecord] = []
        for recommendation_id in action.recommendation_ids:
            record = requested_records.get(recommendation_id)
            if record is None:
                record = await self._recommendation_repository.get_recommendation_by_id(recommendation_id)
            if record is None or record.id != recommendation_id:
                raise AutomationPlanError(
                    AutomationErrorCode.REQUIRES_CONTAINER_TARGET,
                    "one or more persisted recommendations for the grouped action were not found",
                )
            if record.status is not RecommendationStatus.ACTIVE or record.scope != "workload":
                raise AutomationPlanError(
                    AutomationErrorCode.REQUIRES_CONTAINER_TARGET,
                    "all recommendations in the grouped action must remain active workload recommendations",
                )
            if record.action is None or record.action.model_dump(mode="json") != action_data:
                raise AutomationPlanError(
                    AutomationErrorCode.REQUIRES_CONTAINER_TARGET,
                    "recommendations do not share one persisted action",
                )
            records.append(record)

        target = action.target
        for record in records:
            if (
                record.namespace != target.namespace
                or record.owner_kind != target.kind
                or record.owner_name != target.name
            ):
                raise AutomationPlanError(
                    AutomationErrorCode.AMBIGUOUS_BINDING,
                    "persisted recommendation target does not match its stored action",
                )

        binding = await self._binding_repository.get_workload_binding(
            self._cluster,
            target.namespace,
            target.kind,
            target.name,
        )
        if binding is None or binding.status is not BindingStatus.RESOLVED or binding.source is None:
            raise AutomationPlanError(
                AutomationErrorCode.AMBIGUOUS_BINDING,
                "a currently resolved GitOps workload binding is required to create a plan",
            )
        source = _with_binding_policy_evidence(binding.source, binding)
        if (
            source.cluster != self._cluster
            or source.namespace != target.namespace
            or source.workload_kind != target.kind
            or source.workload_name != target.name
            or source.status is not BindingStatus.RESOLVED
        ):
            raise AutomationPlanError(
                AutomationErrorCode.AMBIGUOUS_BINDING,
                "resolved GitOps binding does not match the persisted recommendation target",
            )
        now = _as_utc(self._policy_evaluator.clock())
        if (binding.expires_at is not None and _as_utc(binding.expires_at) <= now) or (
            source.expires_at is not None and _as_utc(source.expires_at) <= now
        ):
            raise AutomationPlanError(
                AutomationErrorCode.SOURCE_CHANGED,
                "the resolved workload binding expired; refresh GitOps discovery before retrying",
            )
        return action, source

    async def _evaluate_policy(
        self,
        recommendation_ids: Sequence[int],
        source: WorkloadSource,
        *,
        exclude_plan_id: str | None = None,
        exclude_plan_ids: Sequence[str] = (),
    ) -> AutomationPolicyResult:
        records: list[RecommendationRecord] = []
        for recommendation_id in recommendation_ids:
            try:
                record = await self._recommendation_repository.get_recommendation_by_id(recommendation_id)
            except Exception:
                record = None
            if record is None or record.id != recommendation_id:
                return await self._policy_evaluator.evaluate(
                    [],
                    source,
                    exclude_plan_id=exclude_plan_id,
                    exclude_plan_ids=exclude_plan_ids,
                )
            records.append(record)
        return await self._policy_evaluator.evaluate(
            records,
            source,
            exclude_plan_id=exclude_plan_id,
            exclude_plan_ids=exclude_plan_ids,
        )

    @staticmethod
    def _ensure_policy_allowed(policy_result: AutomationPolicyResult) -> None:
        if policy_result.allowed:
            return
        rejection = policy_result.rejections[0]
        raise AutomationPlanError(
            rejection.code,
            rejection.detail,
            policy_result=policy_result.as_dict(),
        )

    async def _persist_policy_result(
        self,
        plan: Plan,
        policy_result: AutomationPolicyResult,
        *,
        actor: str,
    ) -> Plan:
        try:
            updated = await self._plan_service.update_policy_result(
                plan.plan_id,
                policy_result.as_dict(),
                expected_digest=plan.digest or "",
                expected_statuses=(PlanStatus.VALIDATED, PlanStatus.APPROVED),
                actor=actor,
            )
        except Exception as exc:
            raise AutomationPlanError(
                AutomationErrorCode.POLICY_EVIDENCE_MISSING,
                "plan storage cannot persist the current policy evaluation; "
                "restore policy-result storage before retrying",
                policy_result=policy_result.as_dict(),
            ) from exc
        if updated is None:
            raise AutomationPlanError(
                AutomationErrorCode.PLAN_STALE,
                "the plan changed while policy was being revalidated; read the plan and retry approval",
                policy_result=policy_result.as_dict(),
            )
        return updated

    async def read_plan(self, plan_id: str) -> Plan | None:
        return await self._plan_service.read_plan(plan_id)

    async def read_operation(self, operation_id: str) -> Operation | None:
        if self.worker is None:
            return None
        return await self.worker.operations.get_by_operation_id(operation_id)

    async def read_operation_for_plan(self, plan_id: str) -> Operation | None:
        if self.worker is None:
            return None
        return await self.worker.operations.get_by_plan_id(plan_id)

    async def prepare_rollback(self, plan_id: str) -> Plan:
        """Persist a rollback preview from a synced plan whose recommendations need review."""
        original_plan = await self._plan_service.read_plan(plan_id)
        if original_plan is None:
            raise AutomationPlanError(AutomationErrorCode.PLAN_STALE, f"plan {plan_id!r} was not found")
        await self._validate_rollback_source_plan(original_plan)
        await self._rollback_review_records(original_plan)
        source = await self._resolve_current_rollback_source(original_plan)
        if source.renderer not in {
            RendererKind.RAW,
            RendererKind.KUSTOMIZE,
            RendererKind.HELM_VALUES,
            RendererKind.HELM_RELEASE,
        }:
            raise AutomationPlanError(
                AutomationErrorCode.ROLLBACK_UNSUPPORTED,
                f"renderer {source.renderer.value!r} does not support exact-target rollback",
            )

        rollback_renderer = self._rollback_renderer
        if source.renderer is RendererKind.RAW and getattr(rollback_renderer, "renderer", None) is not RendererKind.RAW:
            rollback_renderer = None
        if source.renderer is RendererKind.KUSTOMIZE and (
            rollback_renderer is None
            or getattr(rollback_renderer, "renderer", None) is not RendererKind.KUSTOMIZE
            or getattr(rollback_renderer, "supports_exact_target_rollback", False) is not True
        ):
            raise AutomationPlanError(
                AutomationErrorCode.ROLLBACK_UNSUPPORTED,
                f"renderer {source.renderer.value!r} has no configured exact-target rollback adapter",
            )
        if source.renderer is RendererKind.HELM_VALUES:
            if source.controller_kind is not ControllerKind.ARGOCD:
                raise AutomationPlanError(
                    AutomationErrorCode.ROLLBACK_UNSUPPORTED,
                    "HELM_VALUES rollback is supported only for Argo CD sources",
                )
            if getattr(rollback_renderer, "renderer", None) is not RendererKind.HELM_VALUES:
                if self._helm_values_planner is None:
                    raise AutomationPlanError(
                        AutomationErrorCode.ROLLBACK_UNSUPPORTED,
                        "HELM_VALUES rollback has no configured exact-target planner",
                    )
                rollback_renderer = HelmValuesRollbackRenderer(
                    self._helm_values_planner,
                    original_plan.action,
                    snapshot_limits=self._snapshot_limits,
                    workspace_parent=self._snapshot_workspace_parent,
                )
            if getattr(rollback_renderer, "supports_exact_target_rollback", False) is not True:
                raise AutomationPlanError(
                    AutomationErrorCode.ROLLBACK_UNSUPPORTED,
                    "HELM_VALUES rollback adapter does not guarantee exact-target rendering",
                )
        if source.renderer is RendererKind.HELM_RELEASE:
            if source.controller_kind is not ControllerKind.FLUX_HELM_RELEASE:
                raise AutomationPlanError(
                    AutomationErrorCode.ROLLBACK_UNSUPPORTED,
                    "HELM_RELEASE rollback is supported only for Flux HelmRelease sources",
                )
            if self._flux_helm_release_planner is None:
                raise AutomationPlanError(
                    AutomationErrorCode.ROLLBACK_UNSUPPORTED,
                    "HELM_RELEASE rollback has no configured constrained Flux planner",
                )
            original_file_plan = original_plan.file_plan
            if original_file_plan is None or len(original_file_plan.files) != 1:
                raise AutomationPlanError(
                    AutomationErrorCode.ROLLBACK_UNSUPPORTED,
                    "HELM_RELEASE rollback may restore only its original Git-owned HelmRelease file",
                )
            rollback_renderer = FluxHelmReleaseRollbackRenderer(
                self._flux_helm_release_planner,
                original_plan.action,
                original_file_plan.files[0].path,
                snapshot_limits=self._snapshot_limits,
                workspace_parent=self._snapshot_workspace_parent,
            )

        snapshot = await self._snapshot_resolver.create_snapshot(source)
        try:
            metadata = snapshot.metadata
            if metadata.provider_id != source.provider_id or metadata.repo_url != source.repo_url:
                raise AutomationPlanError(
                    AutomationErrorCode.SOURCE_CHANGED,
                    "current rollback snapshot does not match the resolved provider repository",
                )
            revision = metadata.commit_sha
            if not isinstance(revision, str) or not _COMMIT_SHA.fullmatch(revision):
                raise AutomationPlanError(
                    AutomationErrorCode.PROVIDER_NOT_READY,
                    "current rollback snapshot does not identify an immutable full Git commit SHA",
                )
            original_contents = await self._load_original_rollback_contents(original_plan, source)
            try:
                inverse = build_inverse_plan(
                    original_plan,
                    original_contents,
                    snapshot,
                    revision,
                    renderer=rollback_renderer,
                    current_source=(
                        source if source.renderer in {RendererKind.HELM_VALUES, RendererKind.HELM_RELEASE} else None
                    ),
                )
            except UnsupportedRollbackError as exc:
                raise AutomationPlanError(AutomationErrorCode.ROLLBACK_UNSUPPORTED, str(exc)) from exc
            except RollbackValidationError as exc:
                raise AutomationPlanError(AutomationErrorCode.RENDER_VALIDATION_FAILED, str(exc)) from exc
            except RollbackConflictError as exc:
                raise AutomationPlanError(AutomationErrorCode.ROLLBACK_CONFLICT, str(exc)) from exc
            except RollbackPlanError as exc:
                raise AutomationPlanError(AutomationErrorCode.ROLLBACK_CONFLICT, str(exc)) from exc
        finally:
            snapshot.close()

        await self._rollback_review_records(original_plan)
        plan_source = source.model_copy(update={"revision": revision, "source_revision": revision})
        policy_result = await self._evaluate_policy(
            original_plan.recommendation_ids,
            plan_source,
            exclude_plan_id=original_plan.plan_id,
        )
        self._ensure_policy_allowed(policy_result)
        rollback_plan = await self._plan_service.create_inverse_plan(
            original_plan,
            plan_source,
            inverse,
            policy_result=policy_result.as_dict(),
        )
        await self._rollback_review_records(original_plan)
        return rollback_plan

    async def _validate_rollback_source_plan(self, plan: Plan) -> None:
        if (
            plan.status not in {PlanStatus.SYNCED, PlanStatus.APPLIED}
            or plan.file_plan is None
            or plan.digest is None
            or plan.file_plan.digest != plan.digest
        ):
            raise AutomationPlanError(
                AutomationErrorCode.ROLLBACK_NOT_READY,
                "rollback requires a synced original plan with a matching validated digest",
            )
        if not plan.recommendation_ids or set(plan.recommendation_ids) != set(plan.action.recommendation_ids):
            raise AutomationPlanError(
                AutomationErrorCode.ROLLBACK_NOT_READY,
                "original plan recommendation links are incomplete",
            )
        if plan.source.renderer is not plan.file_plan.renderer:
            raise AutomationPlanError(
                AutomationErrorCode.ROLLBACK_CONFLICT,
                "original plan source and file plan use different renderers",
            )
        if plan.source.renderer is RendererKind.HELM_VALUES:
            try:
                helm_values_inverse_action(plan)
            except UnsupportedRollbackError as exc:
                raise AutomationPlanError(AutomationErrorCode.ROLLBACK_UNSUPPORTED, str(exc)) from exc
            except RollbackConflictError as exc:
                raise AutomationPlanError(AutomationErrorCode.ROLLBACK_NOT_READY, str(exc)) from exc
        elif plan.source.renderer is RendererKind.HELM_RELEASE:
            try:
                flux_helm_release_inverse_baseline(plan)
            except UnsupportedRollbackError as exc:
                raise AutomationPlanError(AutomationErrorCode.ROLLBACK_UNSUPPORTED, str(exc)) from exc
            except RollbackConflictError as exc:
                raise AutomationPlanError(AutomationErrorCode.ROLLBACK_NOT_READY, str(exc)) from exc

    async def _rollback_review_records(self, plan: Plan) -> list[RecommendationRecord]:
        records: list[RecommendationRecord] = []
        expected_action = plan.action.model_dump(mode="json")
        operations = plan.action.operations
        if not operations or any(not isinstance(operation, SetContainerResources) for operation in operations):
            raise AutomationPlanError(
                AutomationErrorCode.ROLLBACK_UNSUPPORTED,
                "only per-container resource actions have a supported rollback workflow",
            )
        operation_containers = {
            operation.container for operation in operations if isinstance(operation, SetContainerResources)
        }
        for recommendation_id in plan.recommendation_ids:
            record = await self._recommendation_repository.get_recommendation_by_id(recommendation_id)
            if (
                record is None
                or record.id != recommendation_id
                or record.status is not RecommendationStatus.ROLLBACK_REVIEW
                or record.scope != "workload"
                or record.action is None
                or record.action.model_dump(mode="json") != expected_action
                or record.namespace != plan.action.target.namespace
                or record.owner_kind != plan.action.target.kind
                or record.owner_name != plan.action.target.name
                or record.container_name not in operation_containers
            ):
                raise AutomationPlanError(
                    AutomationErrorCode.ROLLBACK_NOT_READY,
                    "all recommendations linked to the original plan must remain in rollback_review",
                )
            records.append(record)
        if {record.container_name for record in records} != operation_containers:
            raise AutomationPlanError(
                AutomationErrorCode.ROLLBACK_NOT_READY,
                "linked recommendations do not cover every per-container rollback operation",
            )
        return records

    async def _resolve_current_rollback_source(self, plan: Plan) -> WorkloadSource:
        target = plan.action.target
        binding = await self._binding_repository.get_workload_binding(
            self._cluster,
            target.namespace,
            target.kind,
            target.name,
        )
        if binding is None or binding.status is not BindingStatus.RESOLVED or binding.source is None:
            raise AutomationPlanError(
                AutomationErrorCode.AMBIGUOUS_BINDING,
                "a currently resolved GitOps workload binding is required for rollback",
            )
        source = binding.source
        if not same_rollback_source(plan.source, source):
            raise AutomationPlanError(
                AutomationErrorCode.SOURCE_CHANGED,
                "the resolved workload source no longer matches the original plan",
            )
        source = _with_binding_policy_evidence(source, binding)
        now = _as_utc(self._plan_service.clock())
        if (binding.expires_at is not None and _as_utc(binding.expires_at) <= now) or (
            source.expires_at is not None and _as_utc(source.expires_at) <= now
        ):
            raise AutomationPlanError(
                AutomationErrorCode.SOURCE_CHANGED,
                "the resolved workload source has expired",
            )
        return source

    async def _load_original_rollback_contents(
        self,
        plan: Plan,
        source: WorkloadSource,
    ) -> dict[str, str | bytes]:
        file_plan = plan.file_plan
        assert file_plan is not None
        if not _COMMIT_SHA.fullmatch(file_plan.base_revision):
            raise AutomationPlanError(
                AutomationErrorCode.ROLLBACK_CONFLICT,
                "original plan base revision is not an immutable full Git commit SHA",
            )
        resolver = self._plan_service.git_blob_resolver
        if resolver is None:
            raise AutomationPlanError(
                AutomationErrorCode.PROVIDER_NOT_READY,
                "an authorized Git blob resolver is required to read original rollback contents",
            )
        contents: dict[str, str | bytes] = {}
        for edit in file_plan.files:
            if edit.operation == "delete":
                raise AutomationPlanError(
                    AutomationErrorCode.ROLLBACK_UNSUPPORTED,
                    "forward delete file edits are not supported for rollback",
                )
            if edit.operation != "update":
                continue
            try:
                original = await resolver.get_blob_file(source, edit.path, file_plan.base_revision)
            except AutomationPlanError:
                raise
            except Exception as exc:
                raise AutomationPlanError(
                    AutomationErrorCode.PROVIDER_NOT_READY,
                    "authorized provider could not read an original file at the plan base revision",
                ) from exc
            if original is None or original.path != edit.path or edit.original_sha is None:
                raise AutomationPlanError(
                    AutomationErrorCode.SOURCE_CHANGED,
                    "an original plan source file is missing at its immutable base revision",
                )
            if original.blob_sha.lower() != edit.original_sha.lower():
                raise AutomationPlanError(
                    AutomationErrorCode.ROLLBACK_CONFLICT,
                    "an original source blob no longer matches the persisted plan",
                )
            contents[edit.path] = original.content
        return contents

    async def approve_plan(self, plan_id: str, digest: str, *, actor: str = "system") -> Plan:
        plan = await self._plan_service.read_plan(plan_id)
        additional_excluded_plan_ids: tuple[str, ...] = ()
        policy_source = plan.source if plan is not None else None
        if plan is not None and plan.rollback_of_plan_id is None and plan.plan_id.startswith("rbk-"):
            raise AutomationPlanError(
                AutomationErrorCode.ROLLBACK_CONFLICT,
                "rollback plan is missing its persisted original-plan link",
            )
        if plan is not None and plan.rollback_of_plan_id is not None:
            original = await self._plan_service.read_plan(plan.rollback_of_plan_id)
            if original is None:
                raise AutomationPlanError(
                    AutomationErrorCode.ROLLBACK_CONFLICT,
                    "the original plan linked to this rollback plan no longer exists",
                )
            if (
                original.digest is None
                or plan.file_plan is None
                or plan.plan_id != inverse_plan_id(original.plan_id, original.digest, plan.file_plan.base_revision)
            ):
                raise AutomationPlanError(
                    AutomationErrorCode.ROLLBACK_CONFLICT,
                    "rollback identity does not match its original plan and current base revision",
                )
            await self._validate_rollback_source_plan(original)
            if set(original.recommendation_ids) != set(plan.recommendation_ids):
                raise AutomationPlanError(
                    AutomationErrorCode.ROLLBACK_CONFLICT,
                    "rollback plan recommendation links do not match the original plan",
                )
            await self._rollback_review_records(original)
            policy_source = await self._resolve_current_rollback_source(original)
            additional_excluded_plan_ids = (original.plan_id,)
            if not _same_workload_source_version(plan.source, policy_source):
                raise AutomationPlanError(
                    AutomationErrorCode.SOURCE_CHANGED,
                    "the resolved workload binding changed after the rollback plan was created",
                )
        elif plan is not None:
            policy_source = plan.source
            if plan.status in {PlanStatus.VALIDATED, PlanStatus.APPROVED} and plan.digest == digest:
                _, current_source = await self._resolve_request(plan.recommendation_ids)
                if not _same_workload_source_version(plan.source, current_source):
                    raise AutomationPlanError(
                        AutomationErrorCode.SOURCE_CHANGED,
                        "the resolved workload binding changed after the plan was created",
                    )
                policy_source = current_source

        if plan is not None and plan.status in {PlanStatus.VALIDATED, PlanStatus.APPROVED} and plan.digest == digest:
            assert policy_source is not None
            policy_result = await self._evaluate_policy(
                plan.recommendation_ids,
                policy_source,
                exclude_plan_id=plan.plan_id,
                exclude_plan_ids=additional_excluded_plan_ids,
            )
            await self._persist_policy_result(plan, policy_result, actor=actor)
            self._ensure_policy_allowed(policy_result)
        return await self._plan_service.approve_plan(plan_id, digest, actor=actor)

    async def mark_stale(
        self,
        plan_id: str,
        *,
        actor: str = "system",
        reason_code: str | None = None,
    ) -> Plan | None:
        return await self._plan_service.mark_stale(plan_id, actor=actor, reason_code=reason_code)

    async def expire_plan(self, plan_id: str, *, now: Any = None) -> Plan | None:
        return await self._plan_service.expire_plan(plan_id, now=now)

    async def expire_due_plans(self, *, now: Any = None) -> list[Plan]:
        return await self._plan_service.expire_due_plans(now=now)

    async def reconcile_exhausted_operations(self, *, limit: int = 100) -> list[Plan]:
        """Fail plans whose terminal failed operation can no longer be retried."""
        if self.worker is None:
            return []
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be a positive integer")
        repository = self._plan_service.repository
        try:
            plans = await repository.list_plans(status=PlanStatus.APPLYING)
        except Exception:
            return []

        reconciled: list[Plan] = []
        for plan in plans[:limit]:
            if plan.status is not PlanStatus.APPLYING or plan.digest is None:
                continue
            try:
                operation = await self.worker.operations.get_by_plan_id(plan.plan_id)
            except Exception:
                continue
            if operation is None or operation.status not in {OperationStatus.FAILED, OperationStatus.CANCELLED}:
                continue
            if await self._plan_has_pull_request(plan.plan_id):
                continue
            updated = await repository.set_plan_status(
                plan.plan_id,
                PlanStatus.FAILED,
                expected_statuses=(PlanStatus.APPLYING,),
                expected_digest=plan.digest,
                actor="automation-reconciler",
                reason_code="operation_retry_exhausted",
            )
            if updated is None:
                continue
            await repository.set_recommendation_action_status(
                plan.recommendation_ids,
                ActionStatus.FAILED,
                expected_statuses=(ActionStatus.PLAN_READY, ActionStatus.PR_OPEN),
                actor="automation-reconciler",
                plan_id=plan.plan_id,
            )
            reconciled.append(updated)
        return reconciled

    async def _plan_has_pull_request(self, plan_id: str) -> bool:
        repository = getattr(self.worker, "pull_request_repository", None)
        if repository is None:
            return False
        try:
            return await repository.get_pull_request_by_plan_id(plan_id) is not None
        except Exception:
            return True

    async def supersede_plan(self, plan_id: str) -> Plan | None:
        return await self._plan_service.supersede_plan(plan_id)


@dataclass(frozen=True, slots=True)
class PlanRuntime:
    """Fully composed GitOps persistence, provider and plan execution services."""

    providers: tuple[ConfiguredGitProvider, ...]
    gitops_repository: GitOpsRepository
    plan_operations: PlanOperationRepository
    pull_requests: PullRequestRepository
    recommendations: RecommendationRepository
    registry: GitProviderRegistry
    provider_resolver: RegistryPlanProviderResolver
    revision_resolver: RegistryPlanRevisionResolver
    blob_resolver: AuthorizedGitBlobResolver
    snapshot_resolver: ProviderSourceSnapshotResolver
    plan_service: RuntimePlanService
    worker: PlanWorker


def parse_gitops_providers_json(
    value: str | None,
    *,
    allow_empty: bool = False,
) -> tuple[ConfiguredGitProvider, ...]:
    """Parse chart-provided, secret-free provider settings into typed records."""
    if not isinstance(value, str) or not value.strip():
        if allow_empty and (value is None or isinstance(value, str)):
            return ()
        raise PlanProvidersNotConfiguredError(
            "GITOPS_PROVIDERS_JSON is required; configure at least one Git provider, "
            "a Kubernetes Secret name/key reference, and repository allowlist."
        )
    if len(value.encode("utf-8")) > _MAX_PROVIDER_CONFIG_BYTES:
        raise PlanRuntimeConfigurationError("GITOPS_PROVIDERS_JSON exceeds the supported configuration size.")
    try:
        payload = json.loads(value)
    except (json.JSONDecodeError, UnicodeError):
        raise PlanRuntimeConfigurationError(
            "GITOPS_PROVIDERS_JSON must contain valid JSON with a providers array."
        ) from None

    if isinstance(payload, Mapping):
        if set(payload) != {"providers"}:
            raise PlanRuntimeConfigurationError(
                "GITOPS_PROVIDERS_JSON accepts only a providers array; do not include credential values."
            )
        raw_providers = payload.get("providers")
    else:
        raw_providers = payload
    if not isinstance(raw_providers, list):
        raise PlanProvidersNotConfiguredError(
            "GITOPS_PROVIDERS_JSON has no providers; configure a Git provider, "
            "Secret name/key, and repository allowlist."
        )
    if not raw_providers and not allow_empty:
        raise PlanProvidersNotConfiguredError(
            "GITOPS_PROVIDERS_JSON has no providers; configure a Git provider, "
            "Secret name/key, and repository allowlist."
        )
    if len(raw_providers) > _MAX_CONFIGURED_PROVIDERS:
        raise PlanRuntimeConfigurationError(
            f"GITOPS_PROVIDERS_JSON supports at most {_MAX_CONFIGURED_PROVIDERS} providers."
        )

    providers: list[ConfiguredGitProvider] = []
    seen_ids: set[str] = set()
    for raw_provider in raw_providers:
        if not isinstance(raw_provider, Mapping):
            raise PlanRuntimeConfigurationError("Each GITOPS_PROVIDERS_JSON provider entry must be an object.")
        provider = _parse_provider(raw_provider)
        if provider.config.id == UNRESOLVED_PROVIDER_ID:
            raise PlanRuntimeConfigurationError(
                f"provider ID {UNRESOLVED_PROVIDER_ID!r} is reserved for unresolved GitOps sources."
            )
        if provider.config.id in seen_ids:
            raise PlanRuntimeConfigurationError("GITOPS_PROVIDERS_JSON provider IDs must be unique.")
        seen_ids.add(provider.config.id)
        providers.append(provider)
    return tuple(providers)


def _parse_provider(raw: Mapping[str, Any]) -> ConfiguredGitProvider:
    allowed = {
        "id",
        "type",
        "provider_type",
        "apiBaseUrl",
        "api_base_url",
        "tokenSecret",
        "token_secret",
        "secretRef",
        "secret_ref",
        "secret_namespace",
        "secretNamespace",
        "secret_name",
        "secretName",
        "secret_key",
        "secretKey",
        "tokenFile",
        "enabled",
        "repositories",
        "allowlist",
        "repository_allowlist",
        "repositoryAllowlist",
    }
    if set(raw) - allowed:
        raise PlanRuntimeConfigurationError(
            "Provider configuration contains unsupported fields; credentials must be mounted Secrets, not JSON values."
        )

    provider_id = _required_string(raw, "id")
    if _PROVIDER_ID.fullmatch(provider_id) is None:
        raise PlanRuntimeConfigurationError("Provider IDs must use letters, digits, dots, underscores, or hyphens.")

    provider_type = _aliased_value(raw, ("type", "provider_type"), "provider type")
    if not isinstance(provider_type, str) or provider_type.strip().lower() not in _SUPPORTED_PROVIDER_TYPES:
        raise PlanRuntimeConfigurationError("Provider type must be github, gitlab, or gitea.")
    provider_type = provider_type.strip().lower()

    api_base_url = _aliased_value(raw, ("apiBaseUrl", "api_base_url"), "API base URL", required=False)
    if api_base_url is not None:
        if not isinstance(api_base_url, str):
            raise PlanRuntimeConfigurationError("Provider API base URL must be a URL string.")
        api_base_url = api_base_url.strip() or None
        if api_base_url is not None:
            parsed_url = urlsplit(api_base_url)
            if (
                parsed_url.scheme not in {"http", "https"}
                or not parsed_url.hostname
                or parsed_url.username is not None
                or parsed_url.password is not None
            ):
                raise PlanRuntimeConfigurationError(
                    "Provider API base URL must use HTTP(S) and must not contain credentials."
                )

    secret_namespace, secret_name, secret_key = _parse_secret_reference(raw)
    token_file = raw.get("tokenFile")
    token_path_parts = [secret_namespace] if secret_namespace else []
    token_path_parts.extend((secret_name, secret_key))
    expected_token_file = str(DEFAULT_PROVIDER_SECRET_MOUNT.joinpath(*token_path_parts))
    if token_file is not None and (not isinstance(token_file, str) or token_file != expected_token_file):
        raise PlanRuntimeConfigurationError(
            "Provider tokenFile must match its optional namespace and Secret name/key under "
            "/etc/greenkube/git-providers."
        )
    enabled = raw.get("enabled", True)
    if type(enabled) is not bool:
        raise PlanRuntimeConfigurationError("Provider enabled must be a JSON boolean.")

    repositories_value = _aliased_value(
        raw,
        ("repositories", "allowlist", "repository_allowlist", "repositoryAllowlist"),
        "repository allowlist",
    )
    if (
        not isinstance(repositories_value, list)
        or not repositories_value
        or any(
            not isinstance(pattern, str)
            or not pattern
            or pattern != pattern.strip()
            or any(character.isspace() or ord(character) < 32 for character in pattern)
            for pattern in repositories_value
        )
    ):
        raise PlanRuntimeConfigurationError(
            "Each provider requires a non-empty repositories allowlist of exact names or glob patterns."
        )
    repositories = tuple(sorted(set(repositories_value)))

    try:
        config = GitOpsProviderConfig(
            id=provider_id,
            provider_type=provider_type,
            api_base_url=api_base_url,
            secret_namespace=secret_namespace,
            secret_name=secret_name,
            secret_key=secret_key,
            enabled=enabled,
        )
        SecretReference.from_provider_config(config)
    except (ValueError, RuntimeError):
        raise PlanRuntimeConfigurationError(
            "Each Git provider requires a valid Kubernetes Secret name, optional namespace, and key reference."
        ) from None

    return ConfiguredGitProvider(
        config=config,
        repositories=repositories,
        token_file=expected_token_file,
    )


def _parse_secret_reference(raw: Mapping[str, Any]) -> tuple[str | None, str, str]:
    nested_aliases = ("tokenSecret", "token_secret", "secretRef", "secret_ref")
    nested_fields = [key for key in nested_aliases if key in raw]
    flat_fields = [
        key
        for key in (
            "secret_namespace",
            "secretNamespace",
            "secret_name",
            "secretName",
            "secret_key",
            "secretKey",
        )
        if key in raw
    ]
    if len(nested_fields) > 1 or (nested_fields and flat_fields):
        raise PlanRuntimeConfigurationError("Configure one Kubernetes Secret reference per Git provider.")

    if nested_fields:
        nested = raw[nested_fields[0]]
        if not isinstance(nested, Mapping) or set(nested) - {
            "name",
            "key",
            "namespace",
            "secret_name",
            "secret_key",
            "secret_namespace",
        }:
            raise PlanRuntimeConfigurationError(
                "A provider tokenSecret must contain only the Secret name, key, and optional namespace."
            )
        name = _aliased_value(nested, ("name", "secret_name"), "Secret name")
        key = _aliased_value(nested, ("key", "secret_key"), "Secret key")
        namespace = _aliased_value(
            nested,
            ("namespace", "secret_namespace"),
            "Secret namespace",
            required=False,
        )
    else:
        name = _aliased_value(raw, ("secret_name", "secretName"), "Secret name")
        key = _aliased_value(raw, ("secret_key", "secretKey"), "Secret key")
        namespace = _aliased_value(
            raw,
            ("secret_namespace", "secretNamespace"),
            "Secret namespace",
            required=False,
        )

    if not isinstance(name, str) or not name.strip() or not isinstance(key, str) or not key.strip():
        raise PlanRuntimeConfigurationError(
            "Each Git provider requires a Kubernetes Secret name and key; inline credentials are not supported."
        )
    if namespace is not None and (not isinstance(namespace, str) or not namespace.strip()):
        raise PlanRuntimeConfigurationError("A configured Secret namespace must be a non-empty string.")
    return (
        namespace.strip() if isinstance(namespace, str) else None,
        name.strip(),
        key.strip(),
    )


def _required_string(raw: Mapping[str, Any], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise PlanRuntimeConfigurationError(f"Provider {field} must be a non-empty string.")
    return value.strip()


def _aliased_value(
    raw: Mapping[str, Any],
    names: tuple[str, ...],
    label: str,
    *,
    required: bool = True,
) -> Any:
    present = [name for name in names if name in raw]
    if len(present) > 1:
        raise PlanRuntimeConfigurationError(f"Configure only one field for {label}.")
    if not present:
        if required:
            raise PlanRuntimeConfigurationError(f"Provider {label} is required.")
        return None
    return raw[present[0]]


async def reconcile_gitops_providers(
    repository: GitOpsProviderStore,
    providers: Sequence[ConfiguredGitProvider],
) -> None:
    """Upsert provider settings and exactly reconcile their allowlists idempotently."""
    configured_ids = {provider.config.id for provider in providers}
    existing = {provider.id: provider for provider in await repository.list_provider_configs()}

    for provider in providers:
        previous = existing.get(provider.config.id)
        if previous is None or not _same_provider_configuration(previous, provider.config):
            await repository.save_provider_config(provider.config)

        allowlist = set(await repository.list_provider_allowlists(provider.config.id))
        desired = set(provider.repositories)
        for pattern in sorted(allowlist - desired):
            await repository.remove_provider_allowlist(provider.config.id, pattern)
        for pattern in sorted(desired - allowlist):
            await repository.add_provider_allowlist(provider.config.id, pattern)

    for provider_id, provider in existing.items():
        if provider_id in configured_ids:
            continue
        if provider.enabled:
            await repository.save_provider_config(
                replace(
                    provider,
                    enabled=False,
                    status="unverified",
                    last_checked_at=None,
                    last_error_code=None,
                )
            )
        for pattern in await repository.list_provider_allowlists(provider_id):
            await repository.remove_provider_allowlist(provider_id, pattern)


async def initialize_gitops_provider_configs(
    *,
    repository: GitOpsProviderStore | None = None,
    db_manager: Any | None = None,
    config_json: str | None = None,
    required: bool = False,
) -> tuple[ConfiguredGitProvider, ...]:
    """Populate configured providers, allowing non-GitOps service startup without settings."""
    raw = config_json if config_json is not None else os.getenv("GITOPS_PROVIDERS_JSON")
    if raw is None:
        if required:
            parse_gitops_providers_json(raw)
        return ()
    if not isinstance(raw, str):
        raise PlanRuntimeConfigurationError("GITOPS_PROVIDERS_JSON must be a JSON string.")
    if not raw.strip():
        if required:
            parse_gitops_providers_json(raw)
        providers: tuple[ConfiguredGitProvider, ...] = ()
    else:
        try:
            providers = parse_gitops_providers_json(raw, allow_empty=not required)
        except PlanProvidersNotConfiguredError:
            if required:
                raise
            return ()
    if repository is None:
        if db_manager is None:
            from greenkube.core.db import get_db_manager

            db_manager = get_db_manager()
        repository = _make_gitops_repository(db_manager, get_config().DB_TYPE)
    await reconcile_gitops_providers(repository, providers)
    return providers


def parse_pull_request_options_json(value: str | None) -> PullRequestOptions:
    """Parse the documented automation.pullRequests schema from its runtime JSON value."""
    if value is None:
        return DEFAULT_AUTOMATION_PULL_REQUEST_OPTIONS
    if not isinstance(value, str):
        raise PlanRuntimeConfigurationError("GITOPS_PULL_REQUESTS_JSON must be a JSON string.")
    if not value.strip():
        return DEFAULT_AUTOMATION_PULL_REQUEST_OPTIONS
    if len(value.encode("utf-8")) > 16_384:
        raise PlanRuntimeConfigurationError("GITOPS_PULL_REQUESTS_JSON exceeds the supported configuration size.")
    try:
        payload = json.loads(value)
    except (json.JSONDecodeError, UnicodeError):
        raise PlanRuntimeConfigurationError("GITOPS_PULL_REQUESTS_JSON must contain a valid JSON object.") from None
    if not isinstance(payload, Mapping):
        raise PlanRuntimeConfigurationError("GITOPS_PULL_REQUESTS_JSON must contain a valid JSON object.")
    options_data = DEFAULT_AUTOMATION_PULL_REQUEST_OPTIONS.model_dump(by_alias=True)
    options_data.update(payload)
    try:
        return PullRequestOptions.model_validate(options_data)
    except (ValidationError, TypeError, ValueError):
        raise PlanRuntimeConfigurationError(
            "GITOPS_PULL_REQUESTS_JSON must match automation.pullRequests (draft, labels, reviewers, teamReviewers)."
        ) from None


async def create_plan_runtime(
    *,
    config_json: str | None = None,
    policy_config: AutomationPolicyConfig | None = None,
    pull_request_options: PullRequestOptions | None = None,
    secret_resolver: KubernetesSecretResolver | None = None,
    db_manager: Any | None = None,
    gitops_repository: GitOpsRepository | None = None,
    plan_operation_repository: PlanOperationRepository | None = None,
    pull_request_repository: PullRequestRepository | None = None,
    recommendation_repository: RecommendationRepository | None = None,
    rollback_renderer: RollbackRenderer | None = None,
    snapshot_limits: SnapshotLimits | None = None,
    connect_db: bool = True,
) -> PlanRuntime:
    """Build repositories, authorized providers, service and plan worker for production."""
    raw = config_json if config_json is not None else os.getenv("GITOPS_PROVIDERS_JSON")
    providers = parse_gitops_providers_json(raw)
    try:
        selected_policy_config = policy_config or AutomationPolicyConfig.from_json(os.getenv("GITOPS_POLICY_JSON"))
    except ValueError as exc:
        raise PlanRuntimeConfigurationError(f"GitOps automation policy configuration is invalid: {exc}") from None
    selected_pull_request_options = (
        pull_request_options
        if pull_request_options is not None
        else parse_pull_request_options_json(os.getenv("GITOPS_PULL_REQUESTS_JSON"))
    )
    config = get_config()
    if db_manager is None:
        from greenkube.core.db import get_db_manager

        db_manager = get_db_manager()
    if connect_db:
        await db_manager.connect()

    if gitops_repository is None:
        gitops_repository = _make_gitops_repository(db_manager, config.DB_TYPE)
    await reconcile_gitops_providers(gitops_repository, providers)

    if plan_operation_repository is None:
        plan_operation_repository = _make_plan_operation_repository(db_manager, config.DB_TYPE)
    if pull_request_repository is None:
        pull_request_repository = _make_pull_request_repository(db_manager, config.DB_TYPE)
    if recommendation_repository is None:
        recommendation_repository = _make_recommendation_repository(db_manager, config.DB_TYPE)

    selected_secret_resolver = secret_resolver or MountedSecretFileResolver()
    if isinstance(selected_secret_resolver, MountedSecretFileResolver):
        for provider in providers:
            if provider.config.enabled:
                try:
                    selected_secret_resolver.validate_provider_mount(provider.config)
                except SecretResolverError as exc:
                    raise PlanRuntimeConfigurationError(str(exc)) from None

    registry = GitProviderRegistry(gitops_repository, selected_secret_resolver)
    provider_resolver = RegistryPlanProviderResolver(gitops_repository, registry)
    revision_resolver = RegistryPlanRevisionResolver(provider_resolver)
    blob_resolver = AuthorizedGitBlobResolver(provider_resolver)
    snapshot_resolver = ProviderSourceSnapshotResolver(provider_resolver, limits=snapshot_limits)

    plan_service = AutomationPlanService(
        gitops_repository,
        git_blob_resolver=blob_resolver,
        revision_resolver=revision_resolver,
        planner=RawManifestPlanner(),
        operation_enqueuer=cast(PlanOperationEnqueuer, plan_operation_repository),
    )
    worker = PlanWorker(
        operations=plan_operation_repository,
        plans=gitops_repository,
        provider_resolver=provider_resolver,
        revision_resolver=revision_resolver,
        pull_request_repository=pull_request_repository,
        recommendation_repository=recommendation_repository,
        workload_reader=K8sWorkloadReader(),
        pull_request_options=selected_pull_request_options,
        binding_reader=gitops_repository,
    )
    api_plan_service = RuntimePlanService(
        recommendation_repository=recommendation_repository,
        binding_repository=gitops_repository,
        cluster=config.CLUSTER_NAME or "default",
        snapshot_resolver=snapshot_resolver,
        plan_service=plan_service,
        worker=worker,
        rollback_renderer=rollback_renderer,
        snapshot_limits=snapshot_limits,
        policy_config=selected_policy_config,
        policy_repository=gitops_repository,
    )
    return PlanRuntime(
        providers=providers,
        gitops_repository=gitops_repository,
        plan_operations=plan_operation_repository,
        pull_requests=pull_request_repository,
        recommendations=recommendation_repository,
        registry=registry,
        provider_resolver=provider_resolver,
        revision_resolver=revision_resolver,
        blob_resolver=blob_resolver,
        snapshot_resolver=snapshot_resolver,
        plan_service=api_plan_service,
        worker=worker,
    )


async def get_plan_service(**runtime_options: Any) -> RuntimePlanService:
    """Create the source-safe plan API service from the production repositories."""
    runtime = await create_plan_runtime(**runtime_options)
    return runtime.plan_service


async def build_plan_api_service(**runtime_options: Any) -> RuntimePlanService:
    """Build the API plan facade from persisted recommendations and bindings."""
    return await get_plan_service(**runtime_options)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _with_binding_policy_evidence(source: WorkloadSource, binding: WorkloadBinding) -> WorkloadSource:
    policy_keys = (
        "workload_labels",
        "workload",
        "rollout_in_progress",
        "controller_suspended",
        "controller_healthy",
        "hpa_pdb_interactions_checked",
        "hpa_pdb_conflict",
    )
    evidence = dict(source.evidence)
    updated = False
    if binding.generation > 0 and evidence.get("binding_generation") != binding.generation:
        evidence["binding_generation"] = binding.generation
        updated = True
    for key in policy_keys:
        current = evidence.get(key)
        missing = current is None or (key in {"workload_labels", "workload"} and not current)
        if missing and key in binding.evidence:
            evidence[key] = binding.evidence[key]
            updated = True
    return source.model_copy(update={"evidence": evidence}) if updated else source


def _with_one_time_helm_values_mapping(
    action: ActionPlanV3,
    source: WorkloadSource,
    mapping: Mapping[str, Any],
) -> WorkloadSource:
    if source.renderer is not RendererKind.HELM_VALUES or source.controller_kind is not ControllerKind.ARGOCD:
        raise AutomationPlanError(
            AutomationErrorCode.UNSUPPORTED_SOURCE,
            "one-time values mappings are supported only for Argo CD local-chart Helm plans",
        )
    if source.renderer_config.get("values_mapping") is not None:
        raise AutomationPlanError(
            AutomationErrorCode.UNSUPPORTED_SOURCE,
            "a configured Application mapping takes precedence over a one-time plan mapping",
        )
    if len(action.operations) != 1 or not isinstance(action.operations[0], SetContainerResources):
        raise AutomationPlanError(
            AutomationErrorCode.UNSUPPORTED_SOURCE,
            "one-time Helm values mappings require a single container-resource operation",
        )
    operation = action.operations[0]
    if not isinstance(mapping, Mapping) or set(mapping) != {"file_path", "container", "path"}:
        raise AutomationPlanError(
            AutomationErrorCode.UNSUPPORTED_SOURCE,
            "one-time values mappings accept only a Git-managed file, target container, and key path",
        )

    file_path = mapping.get("file_path")
    container = mapping.get("container")
    path = mapping.get("path")
    allowed_file = _resolved_helm_values_mapping_file(source)
    if (
        not isinstance(file_path, str)
        or file_path != allowed_file
        or not isinstance(container, str)
        or container != operation.container
        or not isinstance(path, (list, tuple))
        or not path
        or len(path) > 32
        or any(not isinstance(segment, str) or not segment.strip() or segment != segment.strip() for segment in path)
        or path[-1] != "resources"
    ):
        raise AutomationPlanError(
            AutomationErrorCode.UNSUPPORTED_SOURCE,
            "the one-time values mapping must select a server-resolved file and the exact container resources path",
        )

    renderer_config = dict(source.renderer_config)
    renderer_config["values_mapping"] = {
        "file_path": file_path,
        "container": container,
        "path": list(path),
    }
    evidence = dict(source.evidence)
    evidence[ONE_TIME_HELM_VALUES_MAPPING_EVIDENCE_KEY] = True
    return source.model_copy(update={"renderer_config": renderer_config, "evidence": evidence})


def _resolved_helm_values_mapping_file(source: WorkloadSource) -> str | None:
    raw_values_files = source.renderer_config.get("values_files")
    if raw_values_files is None:
        raw_values_files = []
    if not isinstance(raw_values_files, (list, tuple)):
        return None
    if raw_values_files:
        candidate = raw_values_files[-1]
    else:
        candidate = f"{source.path.rstrip('/')}/values.yaml" if source.path not in {"", "."} else "values.yaml"
    if not isinstance(candidate, str):
        return None
    return safe_repo_path(candidate, allow_root=False)


def _helm_values_mapping_options(
    action: ActionPlanV3,
    source: WorkloadSource,
    snapshot: RepositorySnapshot,
) -> dict[str, list[str]] | None:
    if source.renderer is not RendererKind.HELM_VALUES or source.controller_kind is not ControllerKind.ARGOCD:
        return None
    if len(action.operations) != 1:
        return None
    file_path = _resolved_helm_values_mapping_file(source)
    available_paths = {
        path for file in getattr(snapshot, "files", ()) if isinstance((path := getattr(file, "path", None)), str)
    }
    if file_path is None or file_path not in available_paths:
        return None
    operation = action.operations[0]
    if not isinstance(operation, SetContainerResources):
        return None
    container = operation.container
    if not isinstance(container, str) or not container:
        return None
    return {"files": [file_path], "containers": [container]}


def _same_workload_source_version(expected: WorkloadSource, current: WorkloadSource) -> bool:
    if expected.id != current.id:
        return False
    expected_generation = expected.evidence.get("binding_generation")
    current_generation = current.evidence.get("binding_generation")
    if expected_generation is not None and expected_generation != current_generation:
        return False
    version_fields = (
        "provider_id",
        "repo_url",
        "ref",
        "path",
        "controller_kind",
        "controller_name",
        "controller_namespace",
        "controller_uid",
        "controller_generation",
        "source_uid",
        "source_generation",
        "renderer",
    )
    return all(getattr(expected, field) == getattr(current, field) for field in version_fields) and (
        same_resolved_renderer_config(expected, current)
    )


def _same_provider_configuration(left: GitOpsProviderConfig, right: GitOpsProviderConfig) -> bool:
    return (
        left.id == right.id
        and left.provider_type == right.provider_type
        and left.api_base_url == right.api_base_url
        and left.secret_namespace == right.secret_namespace
        and left.secret_name == right.secret_name
        and left.secret_key == right.secret_key
        and left.enabled == right.enabled
        and left.capabilities == right.capabilities
    )


def _make_gitops_repository(db_manager: Any, db_type: str) -> GitOpsRepository:
    if db_type == "postgres":
        from greenkube.storage.postgres.gitops_repository import PostgresGitOpsRepository

        return PostgresGitOpsRepository(db_manager)
    if db_type == "sqlite":
        from greenkube.storage.sqlite.gitops_repository import SQLiteGitOpsRepository

        return SQLiteGitOpsRepository(db_manager)
    raise PlanRuntimeConfigurationError(f"GitOps storage does not support DB_TYPE {db_type!r}.")


def _make_plan_operation_repository(db_manager: Any, db_type: str) -> PlanOperationRepository:
    if db_type == "postgres":
        from greenkube.storage.postgres.plan_operation_repository import PostgresPlanOperationRepository

        return PostgresPlanOperationRepository(db_manager)
    if db_type == "sqlite":
        from greenkube.storage.sqlite.plan_operation_repository import SQLitePlanOperationRepository

        return SQLitePlanOperationRepository(db_manager)
    raise PlanRuntimeConfigurationError(f"Plan operation storage does not support DB_TYPE {db_type!r}.")


def _make_pull_request_repository(db_manager: Any, db_type: str) -> PullRequestRepository:
    if db_type == "postgres":
        from greenkube.storage.postgres.pull_request_repository import PostgresPullRequestRepository

        return PostgresPullRequestRepository(db_manager)
    if db_type == "sqlite":
        from greenkube.storage.sqlite.pull_request_repository import SQLitePullRequestRepository

        return SQLitePullRequestRepository(db_manager)
    raise PlanRuntimeConfigurationError(f"Pull-request storage does not support DB_TYPE {db_type!r}.")


def _make_recommendation_repository(db_manager: Any, db_type: str) -> RecommendationRepository:
    if db_type == "postgres":
        from greenkube.storage.postgres.recommendation_repository import PostgresRecommendationRepository

        return PostgresRecommendationRepository(db_manager)
    if db_type == "sqlite":
        from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository

        return SQLiteRecommendationRepository(db_manager)
    raise PlanRuntimeConfigurationError(f"Recommendation storage does not support DB_TYPE {db_type!r}.")


async def _current_revision(provider: PlanGitProvider, repository: GitRepository, ref: str) -> str:
    reader = cast(GitRefRevisionReader, provider)
    try:
        return await reader.get_ref_revision(repository, ref)
    except AttributeError:
        raise GitProviderCapabilityError(
            provider.name,
            "get_ref_revision",
            reason="The Git adapter must expose an immutable ref lookup.",
        ) from None
    except GitProviderCapabilityError:
        raise
    except GitProviderError:
        raise
    except Exception:
        raise GitProviderError("Git provider ref lookup failed.") from None


async def _close_provider(provider: PlanGitProvider) -> None:
    try:
        await provider.close()
    except Exception:
        pass
