"""Create and manage digest-addressed GitOps automation plans."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Protocol

from pydantic import ValidationError

from greenkube.automation.git.base import GitProvider
from greenkube.automation.rollback import InversePlan, inverse_plan_id
from greenkube.core.observability import elapsed, record_automation_failure, record_automation_plan, start_timer
from greenkube.gitops.contracts import (
    ActionPlanV3,
    ActionStatus,
    AutomationErrorCode,
    BindingStatus,
    FilePlan,
    Plan,
    PlanStatus,
    RendererKind,
    ValidationResult,
    WorkloadSource,
)
from greenkube.gitops.discovery.common import safe_repo_path
from greenkube.gitops.planning.helm import (
    FluxHelmReleasePlanError,
    FluxHelmReleasePlanner,
    FluxHelmReleaseUnsupportedError,
    HelmPlanError,
    HelmValuesNeedsMappingError,
    HelmValuesPlanner,
    HelmValuesUnsupportedError,
)
from greenkube.gitops.planning.kustomize import KustomizePlanError, KustomizePlanner
from greenkube.gitops.planning.raw_manifest import (
    RawManifestPlanError,
    RawManifestPlanner,
)
from greenkube.gitops.planning.raw_manifest import (
    RepositorySnapshot as RawManifestSnapshot,
)
from greenkube.gitops.renderers import RawRenderError, render_raw
from greenkube.gitops.renderers.helm import HelmError
from greenkube.gitops.renderers.kustomize import KustomizeError
from greenkube.gitops.snapshots.models import (
    RepositorySnapshot as ArchiveRepositorySnapshot,
)
from greenkube.gitops.snapshots.models import (
    SnapshotFile,
)
from greenkube.storage.base_gitops_repository import AutomationPlanRepository

_RAW_MANIFEST_SUFFIXES = frozenset({".json", ".yaml", ".yml"})
_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_GIT_BLOB_SHA_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_REPLACEABLE_STATUSES = frozenset({PlanStatus.DRAFT, PlanStatus.VALIDATED, PlanStatus.APPROVED})
_STALEABLE_STATUSES = frozenset({PlanStatus.DRAFT, PlanStatus.VALIDATED, PlanStatus.APPROVED})
_EXPIREABLE_STATUSES = frozenset({PlanStatus.DRAFT, PlanStatus.VALIDATED, PlanStatus.APPROVED})
_BUSY_STATUSES = frozenset({PlanStatus.APPLYING, PlanStatus.PR_OPEN, PlanStatus.MERGED, PlanStatus.SYNCED})
_REPLANABLE_STATUSES = frozenset(
    {PlanStatus.CLOSED, PlanStatus.FAILED, PlanStatus.STALE, PlanStatus.SUPERSEDED, PlanStatus.REJECTED}
)


class AutomationPlanError(RuntimeError):
    """A plan-service error with the shared GitOps error code."""

    def __init__(
        self,
        code: AutomationErrorCode,
        message: str,
        *,
        policy_result: dict[str, Any] | None = None,
        mapping_options: dict[str, list[str]] | None = None,
    ) -> None:
        self.code = code
        self.detail = message
        self.policy_result = policy_result
        self.mapping_options = mapping_options
        super().__init__(message)


PlanServiceError = AutomationPlanError


@dataclass(frozen=True, slots=True)
class GitBlobFile:
    """File contents paired with the Git object ID of the file blob."""

    path: str
    content: str
    blob_sha: str


class GitBlobFileResolver(Protocol):
    """Looks up an immutable Git file and its actual blob SHA at one revision."""

    async def get_blob_file(self, source: WorkloadSource, path: str, revision: str) -> GitBlobFile | None: ...


class PlanRevisionResolver(Protocol):
    """Resolve the current immutable base revision for a plan's source."""

    async def resolve_current_revision(self, source: WorkloadSource) -> str | None: ...


class PlanOperationEnqueuer(Protocol):
    """Enqueues approved plans; implementations must deduplicate by plan ID and digest."""

    async def enqueue(self, plan: Plan, *, idempotency_key: str) -> object: ...


class ProviderGitBlobResolver:
    """Resolve Git blob metadata where the existing provider adapter exposes a blob SHA."""

    def __init__(self, providers: Mapping[str, GitProvider]) -> None:
        self._providers = dict(providers)

    async def get_blob_file(self, source: WorkloadSource, path: str, revision: str) -> GitBlobFile | None:
        provider = self._providers.get(source.provider_id)
        if provider is None:
            raise AutomationPlanError(
                AutomationErrorCode.PROVIDER_NOT_READY,
                f"no Git provider is configured for {source.provider_id!r}",
            )
        if provider.name not in {"github", "gitea", "gitlab"}:
            raise AutomationPlanError(
                AutomationErrorCode.PROVIDER_NOT_READY,
                f"provider {provider.name!r} has no verified Git blob SHA adapter",
            )
        repository = provider.repository(source.repo_url, base_branch=source.ref)
        git_file = await provider.get_file(repository, path, revision)
        if git_file is None:
            return None
        blob_sha = git_file.blob_sha
        if blob_sha is None and provider.name in {"github", "gitea"}:
            blob_sha = git_file.sha
        if blob_sha is None:
            raise AutomationPlanError(
                AutomationErrorCode.PROVIDER_NOT_READY,
                f"provider returned no Git blob SHA for {path!r} at {revision}",
            )
        return GitBlobFile(path=git_file.path, content=git_file.content, blob_sha=blob_sha)


class AutomationPlanService:
    """Validate, persist and transition raw-manifest plans without mutating a cluster."""

    def __init__(
        self,
        repository: AutomationPlanRepository,
        *,
        git_blob_resolver: GitBlobFileResolver | None = None,
        revision_resolver: PlanRevisionResolver | None = None,
        planner: RawManifestPlanner | None = None,
        kustomize_planner: KustomizePlanner | None = None,
        planners: Mapping[RendererKind, Any] | None = None,
        operation_enqueuer: PlanOperationEnqueuer | None = None,
        plan_ttl: timedelta = timedelta(minutes=30),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if plan_ttl <= timedelta(0):
            raise ValueError("plan_ttl must be positive")
        self.repository = repository
        self.git_blob_resolver = git_blob_resolver
        self.revision_resolver = revision_resolver
        self.planner = planner or RawManifestPlanner()
        self.kustomize_planner = kustomize_planner or KustomizePlanner()
        self.planners: dict[RendererKind, Any] = {
            RendererKind.RAW: self.planner,
            RendererKind.KUSTOMIZE: self.kustomize_planner,
            RendererKind.HELM_VALUES: HelmValuesPlanner(),
            RendererKind.HELM_RELEASE: FluxHelmReleasePlanner(),
        }
        if planners is not None:
            self.planners.update(planners)
        self.operation_enqueuer = operation_enqueuer
        self.plan_ttl = plan_ttl
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def create_plan(
        self,
        action: ActionPlanV3,
        source: WorkloadSource,
        snapshot: ArchiveRepositorySnapshot,
        *,
        policy_result: dict[str, Any] | None = None,
        change_set_id: str | None = None,
        actor: str = "system",
    ) -> Plan:
        timer = start_timer()
        status = "failed"
        try:
            plan = await self._create_plan(
                action,
                source,
                snapshot,
                policy_result=policy_result,
                change_set_id=change_set_id,
                persist=True,
                actor=actor,
            )
            status = plan.status.value
            return plan
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            record_automation_plan(status, source.renderer.value, elapsed(timer))
            if status == "failed":
                record_automation_failure("plan")

    async def preview_plan(
        self,
        action: ActionPlanV3,
        source: WorkloadSource,
        snapshot: ArchiveRepositorySnapshot,
        *,
        policy_result: dict[str, Any] | None = None,
        change_set_id: str | None = None,
        actor: str = "system",
    ) -> Plan:
        """Render and validate a plan in memory without persisting or changing action state."""
        return await self._create_plan(
            action,
            source,
            snapshot,
            policy_result=policy_result,
            change_set_id=change_set_id,
            persist=False,
            actor=actor,
        )

    async def _create_plan(
        self,
        action: ActionPlanV3,
        source: WorkloadSource,
        snapshot: ArchiveRepositorySnapshot,
        *,
        policy_result: dict[str, Any] | None = None,
        change_set_id: str | None = None,
        persist: bool,
        actor: str,
    ) -> Plan:
        now = _as_utc(self.clock())
        self._validate_source(source, now)
        contents, file_metadata, base_revision = _archive_contents(snapshot, source)
        if source.renderer is RendererKind.KUSTOMIZE:
            planner = self.planners.get(RendererKind.KUSTOMIZE)
            if planner is None:
                raise AutomationPlanError(
                    AutomationErrorCode.UNSUPPORTED_SOURCE,
                    "no Kustomize planner is configured",
                )
            try:
                file_plan = planner.plan(action, source, snapshot, base_revision)
            except (KustomizePlanError, KustomizeError, ValidationError) as exc:
                raise _planner_error(exc, renderer=RendererKind.KUSTOMIZE) from exc
        elif source.renderer is RendererKind.HELM_VALUES:
            planner = self.planners.get(RendererKind.HELM_VALUES)
            if planner is None:
                raise AutomationPlanError(
                    AutomationErrorCode.UNSUPPORTED_SOURCE,
                    "no Helm values planner is configured",
                )
            try:
                file_plan = planner.plan(
                    action,
                    source,
                    snapshot,
                    base_revision,
                    mapping=source.renderer_config.get("values_mapping"),
                )
            except HelmValuesNeedsMappingError as exc:
                raise AutomationPlanError(
                    AutomationErrorCode.NEEDS_CHART_MAPPING,
                    "a unique Git-managed Helm values mapping is required for this workload",
                ) from exc
            except HelmValuesUnsupportedError as exc:
                raise AutomationPlanError(
                    AutomationErrorCode.UNSUPPORTED_SOURCE,
                    "the Helm values mapping is unsupported or did not preserve the exact target",
                ) from exc
            except (HelmPlanError, HelmError, ValidationError) as exc:
                raise AutomationPlanError(
                    AutomationErrorCode.RENDER_VALIDATION_FAILED,
                    "Helm values render validation failed",
                ) from exc
        elif source.renderer is RendererKind.HELM_RELEASE:
            planner = self.planners.get(RendererKind.HELM_RELEASE)
            if planner is None:
                raise AutomationPlanError(
                    AutomationErrorCode.UNSUPPORTED_SOURCE,
                    "no Flux HelmRelease planner is configured",
                )
            try:
                file_plan = planner.plan(action, source, snapshot, base_revision)
            except FluxHelmReleaseUnsupportedError as exc:
                raise AutomationPlanError(
                    AutomationErrorCode.UNSUPPORTED_SOURCE,
                    "this Flux HelmRelease source is outside the validated local post-renderer subset",
                ) from exc
            except (FluxHelmReleasePlanError, HelmPlanError, HelmError, ValidationError) as exc:
                raise AutomationPlanError(
                    AutomationErrorCode.RENDER_VALIDATION_FAILED,
                    "Flux HelmRelease render validation failed",
                ) from exc
        else:
            target_path = _find_target_path(contents, action)
            git_file = await self._get_git_blob_file(source, target_path, base_revision)
            original_sha = _validated_blob_sha(git_file, target_path, contents[target_path], file_metadata[target_path])

            raw_snapshot = RawManifestSnapshot(
                contents=contents,
                original_blob_shas={target_path: original_sha},
            )
            try:
                file_plan = self.planners[RendererKind.RAW].plan(action, source, raw_snapshot, base_revision)
            except (RawManifestPlanError, ValidationError) as exc:
                raise _planner_error(exc) from exc

        if not file_plan.validation.passed:
            raise AutomationPlanError(
                AutomationErrorCode.RENDER_VALIDATION_FAILED,
                f"{source.renderer.value} render validation did not pass",
            )
        if file_plan.renderer is not source.renderer or file_plan.validation.renderer is not source.renderer:
            raise AutomationPlanError(
                AutomationErrorCode.RENDER_VALIDATION_FAILED,
                f"{source.renderer.value} planner returned a mismatched renderer validation result",
            )
        if not isinstance(file_plan.plan_id, str) or not file_plan.plan_id.strip():
            raise AutomationPlanError(
                AutomationErrorCode.RENDER_VALIDATION_FAILED,
                f"{source.renderer.value} planner returned a plan without a stable identity",
            )
        try:
            plan = Plan(
                plan_id=file_plan.plan_id,
                recommendation_ids=list(action.recommendation_ids),
                source=source,
                action=action,
                policy_result=dict(policy_result or {}),
                file_plan=file_plan,
                status=PlanStatus.VALIDATED,
                digest=file_plan.digest,
                change_set_id=change_set_id,
                created_at=now,
                expires_at=now + self.plan_ttl,
            )
        except ValidationError as exc:
            raise AutomationPlanError(
                AutomationErrorCode.RENDER_VALIDATION_FAILED,
                f"{source.renderer.value} planner output could not be represented as a validated automation plan",
            ) from exc

        existing = await self.repository.get_plan(plan.plan_id)
        if existing is not None:
            if existing.digest != plan.digest or existing.source.id != source.id:
                raise AutomationPlanError(
                    AutomationErrorCode.PLAN_STALE,
                    "plan ID already belongs to a different source revision or digest; use a new action ID",
                )
            if (
                existing.status in _EXPIREABLE_STATUSES
                and existing.expires_at is not None
                and _as_utc(existing.expires_at) <= now
            ):
                await self._transition(existing, PlanStatus.STALE)
                raise AutomationPlanError(AutomationErrorCode.PLAN_EXPIRED, "plan has expired")
            if existing.status in _REPLANABLE_STATUSES:
                plan = await self._next_attempt_plan(plan)
            elif existing.status not in _REPLACEABLE_STATUSES | {PlanStatus.APPLYING, PlanStatus.PR_OPEN}:
                raise AutomationPlanError(
                    AutomationErrorCode.PLAN_STALE,
                    f"plan cannot be recreated from status {existing.status.value}",
                )
            else:
                return existing

        previous_plans = []
        for previous_status in _BUSY_STATUSES | _REPLACEABLE_STATUSES:
            previous_plans.extend(await self.repository.list_plans(status=previous_status))
        previous_plans = [candidate for candidate in previous_plans if _same_workload_binding(candidate.source, source)]
        previous_plans.sort(key=lambda candidate: (candidate.created_at, candidate.plan_id))
        busy = [item for item in previous_plans if item.status in _BUSY_STATUSES]
        if busy:
            raise AutomationPlanError(
                AutomationErrorCode.PLAN_STALE,
                f"source already has a plan in progress ({busy[0].plan_id})",
            )

        if not persist:
            return plan

        saved = await self.repository.save_plan(plan, policy_result=policy_result, actor=actor)
        for previous in previous_plans:
            if previous.plan_id != saved.plan_id and previous.status in _REPLACEABLE_STATUSES:
                await self._transition(previous, PlanStatus.SUPERSEDED)
        await self.repository.set_recommendation_action_status(
            saved.recommendation_ids,
            ActionStatus.PLAN_READY,
            expected_statuses=(
                ActionStatus.NONE,
                ActionStatus.GROUPED,
                ActionStatus.CLOSED,
                ActionStatus.FAILED,
                ActionStatus.PLAN_READY,
            ),
            actor=actor,
            plan_id=saved.plan_id,
        )
        return saved

    async def _next_attempt_plan(self, plan: Plan) -> Plan:
        if plan.file_plan is None or plan.digest is None:
            raise AutomationPlanError(
                AutomationErrorCode.RENDER_VALIDATION_FAILED,
                "a retryable plan must retain its validated file plan and digest",
            )
        attempt = 2
        while True:
            plan_id = f"{plan.plan_id}-attempt-{attempt}"
            digest = f"sha256:{hashlib.sha256(f'{plan.digest}:{plan_id}'.encode('utf-8')).hexdigest()}"
            existing = await self.repository.get_plan(plan_id)
            if existing is None:
                file_plan = plan.file_plan.model_copy(update={"plan_id": plan_id, "digest": digest})
                return plan.model_copy(update={"plan_id": plan_id, "digest": digest, "file_plan": file_plan})
            if (
                existing.digest == digest
                and existing.source.id == plan.source.id
                and existing.status not in _REPLANABLE_STATUSES
            ):
                return existing
            attempt += 1

    async def read_plan(self, plan_id: str) -> Plan | None:
        return await self.repository.get_plan(plan_id)

    async def update_policy_result(
        self,
        plan_id: str,
        policy_result: dict[str, Any],
        *,
        expected_digest: str,
        expected_statuses: tuple[PlanStatus, ...] | None = None,
        actor: str = "system",
    ) -> Plan | None:
        return await self.repository.set_plan_policy_result(
            plan_id,
            policy_result,
            expected_digest=expected_digest,
            expected_statuses=expected_statuses,
            actor=actor,
        )

    async def create_inverse_plan(
        self,
        original_plan: Plan,
        source: WorkloadSource,
        inverse: InversePlan,
        *,
        policy_result: dict[str, Any] | None = None,
    ) -> Plan:
        """Persist an exact inverse as a normal validated plan requiring approval."""
        original_file_plan = original_plan.file_plan
        if (
            original_file_plan is None
            or original_plan.digest is None
            or original_file_plan.digest != original_plan.digest
            or inverse.original_plan_id != original_plan.plan_id
            or inverse.original_digest != original_plan.digest
            or inverse.plan_id
            != inverse_plan_id(original_plan.plan_id, original_file_plan.digest, inverse.base_revision)
        ):
            raise AutomationPlanError(
                AutomationErrorCode.ROLLBACK_CONFLICT,
                "rollback input no longer matches the persisted original plan",
            )
        if original_plan.status not in {PlanStatus.SYNCED, PlanStatus.APPLIED}:
            raise AutomationPlanError(
                AutomationErrorCode.ROLLBACK_NOT_READY,
                "only a synced original plan can be rolled back",
            )
        if not inverse.files:
            raise AutomationPlanError(
                AutomationErrorCode.ROLLBACK_NOT_READY,
                "the current source contains no forward-created files or updates to reverse",
            )
        now = _as_utc(self.clock())
        self._validate_source(source, now)
        if source.renderer is not inverse.renderer:
            raise AutomationPlanError(
                AutomationErrorCode.ROLLBACK_CONFLICT,
                "rollback renderer does not match the currently resolved source",
            )

        target_changed = inverse.rendered_before_digest != inverse.rendered_after_digest
        validation = ValidationResult(
            target_changed=target_changed,
            unrelated_changes=False,
            requests_within_limits=original_file_plan.validation.requests_within_limits,
            renderer=inverse.renderer,
            output_digest=inverse.rendered_after_digest,
            warnings=list(inverse.warnings),
            reason="exact inverse render validated against the original resource target",
        )
        if not validation.passed:
            raise AutomationPlanError(
                AutomationErrorCode.RENDER_VALIDATION_FAILED,
                "inverse render did not pass the original plan validation gates",
            )
        file_plan = FilePlan(
            plan_id=inverse.plan_id,
            files=list(inverse.files),
            diff=inverse.diff,
            rendered_diff=inverse.rendered_diff,
            base_revision=inverse.base_revision,
            source_revision=inverse.base_revision,
            renderer=inverse.renderer,
            validation=validation,
            warnings=list(inverse.warnings),
            digest=inverse.digest,
        )
        plan = Plan(
            plan_id=inverse.plan_id,
            recommendation_ids=list(original_plan.recommendation_ids),
            source=source,
            action=original_plan.action,
            policy_result=dict(policy_result or {}),
            file_plan=file_plan,
            status=PlanStatus.VALIDATED,
            digest=inverse.digest,
            change_set_id=original_plan.change_set_id,
            rollback_of_plan_id=original_plan.plan_id,
            created_at=now,
            expires_at=now + self.plan_ttl,
        )
        existing = await self.repository.get_plan(plan.plan_id)
        if existing is not None:
            if (
                existing.digest != plan.digest
                or existing.rollback_of_plan_id != original_plan.plan_id
                or existing.source.id != source.id
            ):
                raise AutomationPlanError(
                    AutomationErrorCode.ROLLBACK_CONFLICT,
                    "rollback identity is already bound to different persisted plan content",
                )
            if existing.status in {
                PlanStatus.CLOSED,
                PlanStatus.FAILED,
                PlanStatus.NEEDS_MAPPING,
                PlanStatus.AMBIGUOUS,
                PlanStatus.STALE,
                PlanStatus.REJECTED,
                PlanStatus.SUPERSEDED,
                PlanStatus.SYNC_FAILED,
            }:
                raise AutomationPlanError(
                    AutomationErrorCode.ROLLBACK_CONFLICT,
                    "the existing rollback plan is no longer actionable at this source revision",
                )
            return existing
        busy = [
            candidate
            for candidate in await self.repository.list_plans()
            if candidate.plan_id != original_plan.plan_id
            and _same_workload_binding(candidate.source, source)
            and candidate.status in _BUSY_STATUSES
        ]
        if busy:
            raise AutomationPlanError(
                AutomationErrorCode.ROLLBACK_NOT_READY,
                "another plan is already applying or syncing the resolved workload source",
            )
        saved = await self.repository.save_plan_if_absent(
            plan,
            policy_result=policy_result,
        )
        if (
            saved.digest != plan.digest
            or saved.rollback_of_plan_id != original_plan.plan_id
            or saved.source.id != source.id
        ):
            raise AutomationPlanError(
                AutomationErrorCode.ROLLBACK_CONFLICT,
                "rollback identity is already bound to different persisted plan content",
            )
        if saved.status in {
            PlanStatus.CLOSED,
            PlanStatus.FAILED,
            PlanStatus.NEEDS_MAPPING,
            PlanStatus.AMBIGUOUS,
            PlanStatus.STALE,
            PlanStatus.REJECTED,
            PlanStatus.SUPERSEDED,
            PlanStatus.SYNC_FAILED,
        }:
            raise AutomationPlanError(
                AutomationErrorCode.ROLLBACK_CONFLICT,
                "the existing rollback plan is no longer actionable at this source revision",
            )
        return saved

    async def approve_plan(self, plan_id: str, digest: str, *, actor: str = "system") -> Plan:
        plan = await self.repository.get_plan(plan_id)
        if plan is None:
            raise AutomationPlanError(AutomationErrorCode.PLAN_STALE, f"plan {plan_id!r} was not found")

        now = _as_utc(self.clock())
        if plan.status in _EXPIREABLE_STATUSES and plan.expires_at is not None and _as_utc(plan.expires_at) <= now:
            await self._transition(plan, PlanStatus.STALE, actor=actor, reason_code="plan_expired")
            raise AutomationPlanError(AutomationErrorCode.PLAN_EXPIRED, "plan has expired")
        if plan.status not in {PlanStatus.VALIDATED, PlanStatus.APPROVED}:
            raise AutomationPlanError(
                AutomationErrorCode.PLAN_STALE,
                f"plan cannot be approved from status {plan.status.value}",
            )
        if plan.digest != digest:
            if plan.status is PlanStatus.VALIDATED:
                await self._transition(plan, PlanStatus.STALE)
            raise AutomationPlanError(
                AutomationErrorCode.PLAN_STALE,
                "approval digest does not exactly match the persisted plan digest",
            )
        if plan.file_plan is None or plan.file_plan.digest != plan.digest or not plan.file_plan.validation.passed:
            if plan.status is PlanStatus.VALIDATED:
                await self._transition(plan, PlanStatus.REJECTED, actor=actor, reason_code="render_validation_failed")
            raise AutomationPlanError(
                AutomationErrorCode.RENDER_VALIDATION_FAILED,
                "only a plan with successful render validation and a matching digest can be approved",
            )
        current_revision = await self._resolve_current_revision(plan.source)
        if current_revision.lower() != plan.file_plan.base_revision.lower():
            if plan.status in _STALEABLE_STATUSES:
                await self._transition(plan, PlanStatus.STALE, actor=actor, reason_code="source_changed")
            raise AutomationPlanError(
                AutomationErrorCode.PLAN_STALE,
                "the source base revision moved after this plan was created",
            )

        approved = plan
        if plan.status is PlanStatus.VALIDATED:
            approved = await self.repository.set_plan_status(
                plan.plan_id,
                PlanStatus.APPROVED,
                expected_statuses=(PlanStatus.VALIDATED,),
                expected_digest=plan.digest,
                actor=actor,
            )
            if approved is None:
                raise AutomationPlanError(AutomationErrorCode.PLAN_STALE, "plan changed during approval")

        if self.operation_enqueuer is not None:
            if approved.digest is None:
                raise AutomationPlanError(
                    AutomationErrorCode.RENDER_VALIDATION_FAILED,
                    "an approved plan must retain its validated digest",
                )
            try:
                await self.operation_enqueuer.enqueue(
                    approved,
                    idempotency_key=f"plan:{approved.plan_id}:{approved.digest}",
                )
            except Exception as exc:
                raise AutomationPlanError(
                    AutomationErrorCode.PROVIDER_NOT_READY,
                    "the approved plan could not be enqueued",
                ) from exc
        return approved

    async def mark_stale(
        self,
        plan_id: str,
        *,
        actor: str = "system",
        reason_code: str | None = None,
    ) -> Plan | None:
        plan = await self.repository.get_plan(plan_id)
        if plan is None:
            return None
        if plan.status is PlanStatus.STALE:
            return plan
        if plan.status not in _STALEABLE_STATUSES:
            raise AutomationPlanError(
                AutomationErrorCode.PLAN_STALE,
                f"plan cannot become stale from status {plan.status.value}",
            )
        return await self._transition(plan, PlanStatus.STALE, actor=actor, reason_code=reason_code)

    async def expire_plan(self, plan_id: str, *, now: datetime | None = None) -> Plan | None:
        plan = await self.repository.get_plan(plan_id)
        if plan is None or plan.expires_at is None or plan.status not in _EXPIREABLE_STATUSES:
            return plan
        current_time = _as_utc(now if now is not None else self.clock())
        if _as_utc(plan.expires_at) > current_time:
            return plan
        return await self._transition(plan, PlanStatus.STALE, reason_code="plan_expired")

    async def expire_due_plans(self, *, now: datetime | None = None) -> list[Plan]:
        current_time = _as_utc(now if now is not None else self.clock())
        expired: list[Plan] = []
        for status in _EXPIREABLE_STATUSES:
            for plan in await self.repository.list_plans(status=status):
                if plan.expires_at is None or _as_utc(plan.expires_at) > current_time:
                    continue
                transitioned = await self._transition(plan, PlanStatus.STALE, reason_code="plan_expired")
                expired.append(transitioned)
        return sorted(expired, key=lambda item: (item.created_at, item.plan_id))

    async def prune_retained_history(self, *, cutoff: datetime) -> dict[str, int]:
        return await self.repository.prune_automation_history(cutoff=cutoff)

    async def supersede_plan(self, plan_id: str) -> Plan | None:
        plan = await self.repository.get_plan(plan_id)
        if plan is None:
            return None
        if plan.status is PlanStatus.SUPERSEDED:
            return plan
        if plan.status not in _REPLACEABLE_STATUSES:
            raise AutomationPlanError(
                AutomationErrorCode.PLAN_STALE,
                f"plan cannot be superseded from status {plan.status.value}",
            )
        return await self._transition(plan, PlanStatus.SUPERSEDED, reason_code="replacement_plan")

    async def create(
        self,
        action: ActionPlanV3,
        source: WorkloadSource,
        snapshot: ArchiveRepositorySnapshot,
        *,
        policy_result: dict[str, Any] | None = None,
        change_set_id: str | None = None,
        actor: str = "system",
    ) -> Plan:
        return await self.create_plan(
            action,
            source,
            snapshot,
            policy_result=policy_result,
            change_set_id=change_set_id,
            actor=actor,
        )

    async def read(self, plan_id: str) -> Plan | None:
        return await self.read_plan(plan_id)

    async def approve(self, plan_id: str, digest: str) -> Plan:
        return await self.approve_plan(plan_id, digest)

    async def stale(
        self,
        plan_id: str,
        *,
        actor: str = "system",
        reason_code: str | None = None,
    ) -> Plan | None:
        return await self.mark_stale(plan_id, actor=actor, reason_code=reason_code)

    async def expire(self, plan_id: str, *, now: datetime | None = None) -> Plan | None:
        return await self.expire_plan(plan_id, now=now)

    async def supersede(self, plan_id: str) -> Plan | None:
        return await self.supersede_plan(plan_id)

    async def _get_git_blob_file(self, source: WorkloadSource, path: str, revision: str) -> GitBlobFile:
        resolver = self.git_blob_resolver
        if resolver is None:
            raise AutomationPlanError(
                AutomationErrorCode.PROVIDER_NOT_READY,
                "a Git provider lookup is required; archive SHA-256 metadata is not a Git blob SHA",
            )
        try:
            git_file = await resolver.get_blob_file(source, path, revision)
        except AutomationPlanError:
            raise
        except Exception as exc:
            raise AutomationPlanError(
                AutomationErrorCode.PROVIDER_NOT_READY,
                f"could not resolve Git blob metadata for {path!r} at {revision}",
            ) from exc
        if git_file is None:
            raise AutomationPlanError(
                AutomationErrorCode.SOURCE_CHANGED,
                f"{path!r} is missing from the immutable base revision {revision}",
            )
        return git_file

    async def _resolve_current_revision(self, source: WorkloadSource) -> str:
        resolver = self.revision_resolver
        if resolver is None:
            raise AutomationPlanError(
                AutomationErrorCode.PROVIDER_NOT_READY,
                "a current base revision resolver is required to approve a plan",
            )
        try:
            revision = await resolver.resolve_current_revision(source)
        except Exception as exc:
            raise AutomationPlanError(
                AutomationErrorCode.PROVIDER_NOT_READY,
                "could not resolve the current source base revision",
            ) from exc
        if not isinstance(revision, str) or not _COMMIT_SHA_PATTERN.fullmatch(revision):
            raise AutomationPlanError(
                AutomationErrorCode.PROVIDER_NOT_READY,
                "the current source resolver did not return an immutable full Git commit SHA",
            )
        return revision

    async def _transition(
        self,
        plan: Plan,
        status: PlanStatus,
        *,
        actor: str = "system",
        reason_code: str | None = None,
    ) -> Plan:
        updated = await self.repository.set_plan_status(
            plan.plan_id,
            status,
            expected_statuses=(plan.status,),
            expected_digest=plan.digest,
            actor=actor,
            reason_code=reason_code,
        )
        if updated is None:
            raise AutomationPlanError(AutomationErrorCode.PLAN_STALE, "plan changed during state transition")
        if status in {PlanStatus.STALE, PlanStatus.SUPERSEDED, PlanStatus.REJECTED}:
            await self._clear_unplanned_recommendations(plan, actor=actor)
        return updated

    async def _clear_unplanned_recommendations(self, plan: Plan, *, actor: str) -> None:
        clear_ids: list[int] = []
        for recommendation_id in plan.recommendation_ids:
            current = await self.repository.get_recommendation_action_status(recommendation_id)
            if current is not ActionStatus.PLAN_READY:
                continue
            related = await self.repository.list_plans_for_recommendation(recommendation_id)
            has_replacement = any(
                item.plan_id != plan.plan_id
                and item.status in _REPLACEABLE_STATUSES | {PlanStatus.APPLYING, PlanStatus.PR_OPEN}
                for item in related
            )
            if not has_replacement:
                clear_ids.append(recommendation_id)
        if clear_ids:
            await self.repository.set_recommendation_action_status(
                clear_ids,
                ActionStatus.NONE,
                expected_statuses=(ActionStatus.PLAN_READY,),
                actor=actor,
                plan_id=plan.plan_id,
            )

    @staticmethod
    def _validate_source(source: WorkloadSource, now: datetime) -> None:
        if source.status is BindingStatus.AMBIGUOUS:
            raise AutomationPlanError(
                AutomationErrorCode.AMBIGUOUS_BINDING,
                "an ambiguous workload source cannot be planned",
            )
        if source.status is BindingStatus.STALE:
            raise AutomationPlanError(AutomationErrorCode.SOURCE_CHANGED, "workload source binding is stale")
        if source.status is not BindingStatus.RESOLVED:
            raise AutomationPlanError(
                AutomationErrorCode.UNSUPPORTED_SOURCE,
                f"workload source binding is not resolved ({source.status.value})",
            )
        if source.renderer not in {
            RendererKind.RAW,
            RendererKind.KUSTOMIZE,
            RendererKind.HELM_VALUES,
            RendererKind.HELM_RELEASE,
        }:
            raise AutomationPlanError(
                AutomationErrorCode.UNSUPPORTED_SOURCE,
                f"renderer {source.renderer.value!r} is not supported by the automation plan service",
            )
        if source.expires_at is not None and _as_utc(source.expires_at) <= now:
            raise AutomationPlanError(AutomationErrorCode.SOURCE_CHANGED, "workload source binding has expired")


PlanService = AutomationPlanService


def _archive_contents(
    snapshot: ArchiveRepositorySnapshot,
    source: WorkloadSource,
) -> tuple[dict[str, bytes], dict[str, SnapshotFile], str]:
    if not isinstance(snapshot, ArchiveRepositorySnapshot):
        raise AutomationPlanError(
            AutomationErrorCode.UNSUPPORTED_SOURCE,
            "plan creation requires a gitops.snapshots RepositorySnapshot, not a raw-planner snapshot",
        )
    metadata = snapshot.metadata
    if metadata.provider_id != source.provider_id or metadata.repo_url != source.repo_url:
        raise AutomationPlanError(
            AutomationErrorCode.SOURCE_CHANGED,
            "archive snapshot repository identity does not match the resolved workload source",
        )
    if not isinstance(metadata.commit_sha, str) or not _COMMIT_SHA_PATTERN.fullmatch(metadata.commit_sha):
        raise AutomationPlanError(
            AutomationErrorCode.SOURCE_CHANGED,
            "archive snapshot does not identify an immutable full Git commit SHA",
        )
    try:
        root = snapshot.workspace.resolve(strict=True)
        if not root.is_dir():
            raise OSError("snapshot workspace is not a directory")
    except (OSError, RuntimeError) as exc:
        raise AutomationPlanError(
            AutomationErrorCode.SOURCE_CHANGED,
            "archive snapshot workspace is unavailable",
        ) from exc

    contents: dict[str, bytes] = {}
    file_metadata: dict[str, SnapshotFile] = {}
    seen_paths: set[str] = set()
    explicit_source_paths: set[str] = set()
    explicit_source_prefixes: set[str] = set()
    if source.renderer is RendererKind.HELM_VALUES:
        values_files = source.renderer_config.get("values_files")
        if isinstance(values_files, list):
            explicit_source_paths.update(path for path in values_files if isinstance(path, str))
    elif source.renderer is RendererKind.HELM_RELEASE:
        manifest_source = source.renderer_config.get("manifest_source")
        if isinstance(manifest_source, Mapping):
            manifest_path = manifest_source.get("path")
            if isinstance(manifest_path, str):
                explicit_source_paths.add(manifest_path)
        chart = source.renderer_config.get("chart")
        if (
            isinstance(chart, Mapping)
            and chart.get("source_kind") == "GitRepository"
            and chart.get("repo_url") == source.repo_url
        ):
            raw_chart_path = chart.get("name")
            if not isinstance(raw_chart_path, str):
                raise AutomationPlanError(
                    AutomationErrorCode.UNSUPPORTED_SOURCE,
                    "Flux local Git chart path is missing from the resolved source",
                )
            chart_path = safe_repo_path(raw_chart_path.removeprefix("./"), allow_root=True)
            if chart_path is None:
                raise AutomationPlanError(AutomationErrorCode.UNSAFE_PATH, "Flux local Git chart path is unsafe")
            explicit_source_prefixes.add(chart_path)
    for item in snapshot.files:
        path = item.path
        if not _safe_snapshot_path(path):
            raise AutomationPlanError(
                AutomationErrorCode.UNSAFE_PATH,
                f"archive snapshot contains an unsafe file path {path!r}",
            )
        if path in seen_paths:
            raise AutomationPlanError(
                AutomationErrorCode.AMBIGUOUS_BINDING,
                f"archive snapshot contains duplicate file metadata for {path!r}",
            )
        seen_paths.add(path)
        in_source_scope = _in_binding_scope(path, source.path)
        if source.renderer is RendererKind.RAW:
            if not in_source_scope or PurePosixPath(path).suffix.lower() not in _RAW_MANIFEST_SUFFIXES:
                continue
        elif source.renderer is RendererKind.KUSTOMIZE:
            if PurePosixPath(path).suffix.lower() not in _RAW_MANIFEST_SUFFIXES:
                continue
        elif (
            not in_source_scope
            and path not in explicit_source_paths
            and not any(
                prefix == "." or path == prefix or path.startswith(f"{prefix}/") for prefix in explicit_source_prefixes
            )
        ):
            continue

        current_path = root
        for part in PurePosixPath(path).parts:
            current_path = current_path / part
            if current_path.is_symlink():
                raise AutomationPlanError(
                    AutomationErrorCode.UNSAFE_PATH,
                    f"archive snapshot path {path!r} traverses a symbolic link",
                )
        try:
            resolved_path = current_path.resolve()
            resolved_path.relative_to(root)
            content = current_path.read_bytes()
        except (OSError, RuntimeError, ValueError) as exc:
            raise AutomationPlanError(
                AutomationErrorCode.SOURCE_CHANGED,
                f"snapshot file {path!r} is unavailable or escaped its workspace",
            ) from exc

        actual_digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if len(content) != item.size_bytes or actual_digest != item.digest:
            raise AutomationPlanError(
                AutomationErrorCode.SOURCE_CHANGED,
                f"snapshot file {path!r} no longer matches its archive integrity metadata",
            )
        contents[path] = content
        file_metadata[path] = item

    return contents, file_metadata, metadata.commit_sha


def _find_target_path(contents: Mapping[str, bytes], action: ActionPlanV3) -> str:
    try:
        rendered = render_raw(contents)
    except RawRenderError as exc:
        code = (
            AutomationErrorCode.AMBIGUOUS_BINDING
            if "duplicate resource identity" in str(exc).lower()
            else AutomationErrorCode.RENDER_VALIDATION_FAILED
        )
        raise AutomationPlanError(code, f"raw source render validation failed: {exc}") from exc
    matches = [resource.source_path for resource in rendered.resources if resource.target == action.target]
    if len(matches) > 1:
        raise AutomationPlanError(
            AutomationErrorCode.AMBIGUOUS_BINDING,
            "more than one raw manifest contains the exact target resource",
        )
    if not matches:
        raise AutomationPlanError(
            AutomationErrorCode.RENDER_VALIDATION_FAILED,
            "no raw manifest contains the exact target resource in the binding scope",
        )
    return matches[0]


def _validated_blob_sha(
    git_file: GitBlobFile,
    path: str,
    snapshot_content: bytes,
    snapshot_file: SnapshotFile,
) -> str:
    sha = git_file.blob_sha
    expected_content = snapshot_content
    try:
        provider_content = git_file.content.encode("utf-8")
    except (AttributeError, UnicodeEncodeError) as exc:
        raise AutomationPlanError(
            AutomationErrorCode.PROVIDER_NOT_READY,
            f"provider returned invalid UTF-8 content for {path!r}",
        ) from exc
    if git_file.path != path or provider_content != expected_content:
        raise AutomationPlanError(
            AutomationErrorCode.SOURCE_CHANGED,
            f"provider content for {path!r} does not match the immutable archive snapshot",
        )
    if not isinstance(sha, str) or not _GIT_BLOB_SHA_PATTERN.fullmatch(sha):
        raise AutomationPlanError(
            AutomationErrorCode.PROVIDER_NOT_READY,
            f"provider did not return a valid Git blob SHA for {path!r}; snapshot SHA-256 cannot be substituted",
        )
    snapshot_digest = snapshot_file.digest
    if sha.lower() == snapshot_digest.removeprefix("sha256:").lower():
        raise AutomationPlanError(
            AutomationErrorCode.PROVIDER_NOT_READY,
            f"provider returned the archive content digest instead of a Git blob SHA for {path!r}",
        )
    return sha


def _planner_error(
    error: Exception,
    *,
    renderer: RendererKind = RendererKind.RAW,
) -> AutomationPlanError:
    message = str(error)
    lowered = message.lower()
    if "ambiguous" in lowered or "duplicate resource identity" in lowered:
        code = AutomationErrorCode.AMBIGUOUS_BINDING
    elif "unsupported" in lowered or "must use the raw renderer" in lowered:
        code = AutomationErrorCode.UNSUPPORTED_SOURCE
    elif "container" in lowered and ("missing" in lowered or "ambiguous" in lowered):
        code = AutomationErrorCode.REQUIRES_CONTAINER_TARGET
    else:
        code = AutomationErrorCode.RENDER_VALIDATION_FAILED
    prefix = "Kustomize" if renderer is RendererKind.KUSTOMIZE else "raw manifest"
    return AutomationPlanError(code, f"{prefix} planning failed: {message}")


def _safe_snapshot_path(path: str) -> bool:
    if not isinstance(path, str) or not path or path != path.strip() or "\x00" in path or "\\" in path:
        return False
    parsed = PurePosixPath(path)
    return not parsed.is_absolute() and all(part not in {"", ".", ".."} for part in path.split("/"))


def _in_binding_scope(path: str, scope: str) -> bool:
    return scope == "." or path == scope or path.startswith(f"{scope}/")


def _same_workload_binding(left: WorkloadSource, right: WorkloadSource) -> bool:
    return (
        left.cluster,
        left.namespace,
        left.workload_kind,
        left.workload_name,
    ) == (
        right.cluster,
        right.namespace,
        right.workload_kind,
        right.workload_name,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
