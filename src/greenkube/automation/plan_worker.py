"""Execute approved, digest-addressed GitOps plans from the durable queue."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, Literal, Protocol, TypeVar, cast

from greenkube.automation.git.base import (
    EMPTY_PULL_REQUEST_OPTIONS,
    GitCommitRequest,
    GitCommitResult,
    GitConflictError,
    GitFile,
    GitFileEdit,
    GitProviderCapability,
    GitProviderError,
    GitProviderTransientError,
    GitRepository,
    GitTreeChange,
    GitTreeComparison,
    PullRequestInfo,
    PullRequestOptions,
    git_blob_id,
    parse_repo_url,
)
from greenkube.automation.git.budget import DEFAULT_PLAN_OPERATION_PROVIDER_CALL_BUDGET, provider_call_budget
from greenkube.automation.plans import PlanRevisionResolver
from greenkube.automation.pr_content import (
    PullRequestContext,
    PullRequestMetadata,
    PullRequestRenderSettings,
    derive_metadata,
    render_commit_message,
    render_pull_request,
)
from greenkube.automation.pr_content.model import PullRequestFeatures
from greenkube.automation.rollback import (
    inverse_plan_id,
    same_resolved_renderer_config,
    same_rollback_source,
)
from greenkube.core.observability import (
    elapsed,
    record_automation_failure,
    record_automation_queue_age,
    record_automation_worker_heartbeat,
    record_plan_operation,
    record_plan_operation_duration,
    start_timer,
)
from greenkube.core.optimization.applied_detector import WorkloadReader, WorkloadSnapshot
from greenkube.gitops.contracts import (
    ActionStatus,
    BindingStatus,
    ContainerRequestBaseline,
    FileEdit,
    Operation,
    OperationStatus,
    Plan,
    PlanStatus,
    PreApplyWorkloadBaseline,
    SetContainerResources,
    WorkloadSource,
)
from greenkube.gitops.sha import GIT_OBJECT_SHA, SHA256_DIGEST
from greenkube.models.metrics import (
    PullRequestRecord,
    PullRequestStatus,
    RecommendationRecord,
    RecommendationStatus,
)
from greenkube.storage.base_gitops_repository import GitOpsProviderConfig, WorkloadBinding
from greenkube.storage.base_plan_operation_repository import (
    PlanOperationLease,
    PlanOperationLeaseError,
)
from greenkube.utils.time import ensure_utc

logger = logging.getLogger(__name__)

#: Commit status context published on the PR head (J5).
VALIDATION_STATUS_CONTEXT = "greenkube/validation"
_SUPPORTED_PROVIDER_TYPES = frozenset({"github", "gitlab", "gitea"})
_ACTIVE_PLAN_STATUSES = frozenset({PlanStatus.APPROVED, PlanStatus.APPLYING, PlanStatus.PR_OPEN})
_OPEN_PULL_REQUEST_STATES = frozenset({"open", "opened"})
# Plans a previous worker already started applying; a reclaim first adopts their open PR.
_RECLAIMABLE_PLAN_STATUSES = frozenset({PlanStatus.APPLYING, PlanStatus.PR_OPEN})
_RESULT = TypeVar("_RESULT")


#: Plan status reason of a plan that failed closed because it would change nothing.
NO_EFFECTIVE_CHANGE_REASON = "no_effective_change"


class PlanWorkerError(RuntimeError):
    """A plan execution failure with its retry and lifecycle disposition."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        plan_status: PlanStatus | None = None,
        action_status: ActionStatus | None = None,
        reason_code: str | None = None,
    ) -> None:
        self.retryable = retryable
        self.plan_status = plan_status
        self.action_status = action_status
        self.reason_code = reason_code
        super().__init__(message)


class PlanGitProvider(Protocol):
    """Provider operations required by the plan worker."""

    name: str
    capabilities: frozenset[GitProviderCapability]
    author_name: str
    author_email: str

    def supports(self, capability: GitProviderCapability | str) -> bool: ...

    def validate_pull_request_options(self, options: PullRequestOptions | None = None) -> PullRequestOptions: ...

    async def list_files(self, repo: GitRepository, ref: str) -> list[str]: ...

    async def get_file(self, repo: GitRepository, path: str, ref: str) -> GitFile | None: ...

    async def compare_trees(self, repo: GitRepository, base_revision: str, head_ref: str) -> GitTreeComparison: ...

    async def get_blob_ids(self, repo: GitRepository, ref: str, paths: Sequence[str]) -> dict[str, str | None]: ...

    async def create_branch(self, repo: GitRepository, branch: str, from_ref: str) -> None: ...

    async def create_commit(self, request: GitCommitRequest) -> GitCommitResult: ...

    async def find_pull_request(self, repo: GitRepository, head: str, base: str) -> PullRequestInfo | None: ...

    async def create_pull_request(
        self,
        repo: GitRepository,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
        options: PullRequestOptions | None = None,
    ) -> PullRequestInfo: ...

    async def ensure_pull_request_options(
        self,
        repo: GitRepository,
        pull_request: PullRequestInfo,
        options: PullRequestOptions | None = None,
    ) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class GitProviderTarget:
    """An authorized provider scoped to the plan's repository."""

    provider: PlanGitProvider
    repository: GitRepository


class PlanProviderResolver(Protocol):
    """Resolve an authorized Git provider for a workload source."""

    async def resolve(self, source: WorkloadSource) -> GitProviderTarget: ...


class GitOpsProviderConfigRepository(Protocol):
    """Provider configuration lookup used before any Git API call."""

    async def get_provider_config(self, provider_id: str) -> GitOpsProviderConfig | None: ...


class AuthorizedGitProviderRegistry(Protocol):
    """Registry port that returns a provider scoped to an authorized repository."""

    async def get_provider(self, provider_id: str, repository: GitRepository) -> PlanGitProvider: ...


class PlanOperationQueue(Protocol):
    """Lease-fenced operation methods used by a plan worker."""

    async def get_by_operation_id(self, operation_id: str) -> Operation | None: ...

    async def get_by_plan_id(self, plan_id: str) -> Operation | None: ...

    async def claim_next(self, worker_id: str, *, lease_seconds: int = 900) -> PlanOperationLease | None: ...

    async def heartbeat(self, lease: PlanOperationLease, *, lease_seconds: int = 900) -> PlanOperationLease: ...

    async def mark_succeeded(
        self,
        lease: PlanOperationLease,
        *,
        provider_id: str | None = None,
        commit_sha: str | None = None,
        pull_request_number: int | None = None,
    ) -> Operation: ...

    async def mark_failed(self, lease: PlanOperationLease, error: str, *, retryable: bool = True) -> Operation: ...


class PlanStateRepository(Protocol):
    """Plan and grouped-action status methods used by a plan worker."""

    async def get_plan(self, plan_id: str) -> Plan | None: ...

    async def set_plan_status(
        self,
        plan_id: str,
        status: PlanStatus,
        *,
        expected_statuses: Sequence[PlanStatus] | None = None,
        expected_digest: str | None = None,
        actor: str = "system",
        reason_code: str | None = None,
    ) -> Plan | None: ...

    async def set_plan_pre_apply_baseline_if_absent(
        self,
        plan_id: str,
        baseline: PreApplyWorkloadBaseline,
    ) -> Plan | None: ...

    async def set_recommendation_action_status(
        self,
        recommendation_ids: Sequence[int],
        status: ActionStatus,
        *,
        expected_statuses: Sequence[ActionStatus] | None = None,
        actor: str = "system",
        plan_id: str | None = None,
    ) -> int: ...


class RollbackRecommendationReader(Protocol):
    """Read recommendation state before a rollback commit is applied."""

    async def get_recommendation_by_id(self, rec_id: int) -> RecommendationRecord | None: ...


class PlanPullRequestRepository(Protocol):
    """Legacy recommendation pull-request record methods used by a plan worker."""

    async def get_pull_requests_for_recommendation(self, recommendation_id: int) -> list[PullRequestRecord]: ...

    async def save_pull_request(self, record: PullRequestRecord) -> PullRequestRecord: ...

    async def update_pull_request(
        self,
        pr_id: int,
        updates: dict,
        *,
        expected_statuses: Sequence[str] | None = None,
        expected_plan_id: str | None = None,
    ) -> PullRequestRecord: ...


class PlanBindingReader(Protocol):
    async def get_workload_binding(
        self, cluster: str, namespace: str, workload_kind: str, workload_name: str
    ) -> WorkloadBinding | None: ...


class RegistryPlanProviderResolver:
    """Resolve provider configuration, construct the target and enforce its allowlist."""

    def __init__(
        self,
        provider_repository: GitOpsProviderConfigRepository,
        registry: AuthorizedGitProviderRegistry,
    ) -> None:
        self.provider_repository = provider_repository
        self.registry = registry

    async def resolve(self, source: WorkloadSource) -> GitProviderTarget:
        config = await self.provider_repository.get_provider_config(source.provider_id)
        if config is None:
            raise PlanWorkerError(f"Git provider {source.provider_id!r} is not configured.")
        if not config.enabled:
            raise PlanWorkerError(f"Git provider {source.provider_id!r} is disabled.")

        provider_type = config.provider_type.strip().lower()
        if provider_type not in _SUPPORTED_PROVIDER_TYPES:
            raise PlanWorkerError(f"Git provider type {config.provider_type!r} is not supported.")
        try:
            owner, name = parse_repo_url(source.repo_url, provider=provider_type)
        except GitProviderError:
            raise PlanWorkerError("Plan source repository is invalid.") from None
        repository = GitRepository(
            provider=provider_type,
            owner=owner,
            name=name,
            base_url=config.api_base_url,
            default_branch=source.ref,
        )
        provider = await self.registry.get_provider(source.provider_id, repository)
        return GitProviderTarget(provider=provider, repository=repository)

    async def record_check(self, provider_id: str, status: str, *, error_code: str | None = None) -> None:
        """Persist the provider state observed by a real call (``verified`` or ``error``)."""
        record = getattr(self.provider_repository, "record_provider_check", None)
        if record is not None:
            await record(provider_id, status, error_code=error_code)


@dataclass(slots=True)
class _Execution:
    lease: PlanOperationLease


@dataclass(frozen=True, slots=True)
class _PlanResult:
    target: GitProviderTarget
    branch: str
    commit_sha: str | None
    pull_request: PullRequestInfo


class PlanWorker:
    """Claim and execute one plan operation with fenced queue mutations."""

    #: Upper bound on Git provider HTTP calls for one plan operation (D5).
    provider_call_budget_limit: int = DEFAULT_PLAN_OPERATION_PROVIDER_CALL_BUDGET

    def __init__(
        self,
        *,
        operations: PlanOperationQueue,
        plans: PlanStateRepository,
        provider_resolver: PlanProviderResolver,
        revision_resolver: PlanRevisionResolver | None,
        pull_request_repository: PlanPullRequestRepository,
        recommendation_repository: RollbackRecommendationReader | None = None,
        workload_reader: WorkloadReader | None = None,
        binding_reader: PlanBindingReader | None = None,
        pull_request_options: PullRequestOptions | None = None,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = 900,
        render_settings: PullRequestRenderSettings | None = None,
        pull_request_features: PullRequestFeatures | None = None,
    ) -> None:
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive integer")
        self.operations = operations
        self.plans = plans
        self.provider_resolver = provider_resolver
        self.revision_resolver = revision_resolver
        self.pull_request_repository = pull_request_repository
        self.recommendation_repository = recommendation_repository
        self.workload_reader = workload_reader
        self.binding_reader = binding_reader
        self.pull_request_options = pull_request_options or EMPTY_PULL_REQUEST_OPTIONS
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lease_seconds = lease_seconds
        self._render_settings = render_settings
        self._pull_request_features = pull_request_features

    async def process_once(self, worker_id: str) -> bool:
        """Claim and process a single operation, returning whether one was claimed."""
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must not be blank")
        lease = await self.operations.claim_next(worker_id, lease_seconds=self.lease_seconds)
        if lease is None:
            return False

        execution = _Execution(lease=lease)
        plan: Plan | None = None
        operation_type = "unknown"
        timer = start_timer()
        record_automation_queue_age(
            (datetime.now(timezone.utc) - ensure_utc(lease.operation.created_at)).total_seconds()
        )
        try:
            try:
                await self._heartbeat(execution, force=True)
                plan = await self.plans.get_plan(lease.operation.plan_id)
                if plan is None:
                    raise PlanWorkerError(f"Plan {lease.operation.plan_id!r} was not found.")
                operation_type = _plan_operation_type(plan)

                with provider_call_budget(f"plan_{operation_type}", limit=self.provider_call_budget_limit):
                    result = await self._execute(execution, plan)
            except PlanOperationLeaseError:
                raise
            except Exception as exc:
                await self._finish_failure(execution, plan, exc)
                return True

            try:
                await self._finish_success(execution, plan, result)
            except PlanOperationLeaseError:
                raise
            except Exception as exc:
                await self._finish_failure(
                    execution,
                    plan,
                    PlanWorkerError(f"Could not persist completed plan operation: {exc}", retryable=True),
                )
            return True
        finally:
            record_plan_operation_duration(operation_type, elapsed(timer))

    async def _execute(self, execution: _Execution, plan: Plan) -> _PlanResult:
        operation = execution.lease.operation
        adopted = await self._adopt_open_pull_request(execution, plan)
        if adopted is not None:
            return adopted
        now = ensure_utc(self.clock())
        self._validate_operation_and_plan(operation, plan, now)
        await self._validate_current_binding(plan)
        await self._validate_rollback_review(plan)
        file_plan = plan.file_plan
        if file_plan is None:
            raise PlanWorkerError("Plan has no validated file plan.")

        if self.revision_resolver is None:
            raise PlanWorkerError(
                "A current source revision resolver is required; refusing to apply an unverified plan.",
                plan_status=PlanStatus.FAILED,
                action_status=ActionStatus.FAILED,
            )

        target = await self.provider_resolver.resolve(plan.source)
        try:
            self._validate_target(plan.source, target)
            try:
                target.provider.validate_pull_request_options(self.pull_request_options)
            except GitProviderError as exc:
                raise PlanWorkerError(
                    f"Git provider pull-request preflight failed before repository mutation: {exc}",
                    plan_status=PlanStatus.FAILED,
                    action_status=ActionStatus.FAILED,
                ) from exc
            if not target.provider.supports("complete_file_tree"):
                raise PlanWorkerError(
                    f"Git provider {target.provider.name!r} cannot guarantee a complete repository tree listing.",
                    plan_status=PlanStatus.FAILED,
                    action_status=ActionStatus.FAILED,
                )
            await self._heartbeat(execution)
            try:
                current_revision = await self.revision_resolver.resolve_current_revision(plan.source)
            except GitProviderTransientError as exc:
                raise PlanWorkerError("Current source revision lookup failed temporarily.", retryable=True) from exc
            except GitProviderError as exc:
                raise PlanWorkerError("Current source revision lookup failed.") from exc
            except Exception as exc:
                raise PlanWorkerError(
                    "Could not resolve the current source revision.",
                    retryable=True,
                ) from exc
            if current_revision is None:
                raise PlanWorkerError("Current source revision could not be resolved.", retryable=True)
            if not isinstance(current_revision, str) or not GIT_OBJECT_SHA.fullmatch(current_revision):
                raise PlanWorkerError(
                    "Current source revision is not an immutable full Git commit SHA.",
                    plan_status=PlanStatus.STALE,
                    action_status=ActionStatus.NONE,
                )
            committed_on_revision: str | None = None
            if current_revision.lower() != file_plan.base_revision.lower():
                # Unrelated commits must not stale an approved plan: a moved head is accepted
                # when every file the plan touches is unchanged (D7, BUG-49).
                await self._ensure_touched_files_unchanged(execution, target, plan, current_revision)
                committed_on_revision = current_revision

            persisted_plan = await self.plans.get_plan(plan.plan_id)
            if persisted_plan is None:
                raise PlanWorkerError(f"Plan {operation.plan_id!r} disappeared during source revision validation.")
            self._validate_operation_and_plan(operation, persisted_plan, ensure_utc(self.clock()))
            await self._validate_rollback_review(persisted_plan)
            if not _same_plan_content(plan, persisted_plan):
                raise _replan_error("Persisted plan content changed during source revision validation.")
            plan = persisted_plan

            plan = await self._ensure_pre_apply_baseline(execution, plan)

            if plan.status is PlanStatus.APPROVED:
                await self._validate_current_binding(plan)
                await self._heartbeat(execution, force=True)
                updated_plan = await self.plans.set_plan_status(
                    plan.plan_id,
                    PlanStatus.APPLYING,
                    expected_statuses=(PlanStatus.APPROVED,),
                    expected_digest=plan.digest,
                    actor="automation-worker",
                )
                if updated_plan is None:
                    raise PlanWorkerError(f"Plan {operation.plan_id!r} changed before application.")
                plan = updated_plan

            if committed_on_revision is not None:
                plan = await self._record_committed_on_revision(plan, committed_on_revision)
            return await self._apply_plan(execution, _plan_on_committed_revision(plan), target)
        finally:
            try:
                await target.provider.close()
            except Exception:
                pass

    async def _ensure_touched_files_unchanged(
        self,
        execution: _Execution,
        target: GitProviderTarget,
        plan: Plan,
        revision: str,
    ) -> None:
        """Fail with a re-plan error unless every planned file is unchanged at ``revision``."""
        file_plan = plan.file_plan
        assert file_plan is not None
        for edit in file_plan.files:
            git_file = await self._git_call(execution, target.provider.get_file, target.repository, edit.path, revision)
            if edit.operation == "create":
                if git_file is not None:
                    raise _replan_error(f"{edit.path!r} was created after the plan was rendered; re-plan.")
                continue
            blob_sha = (git_file.blob_sha or git_file.sha) if git_file is not None else None
            if (
                git_file is None
                or git_file.path != edit.path
                or not isinstance(blob_sha, str)
                or edit.original_sha is None
                or blob_sha.lower() != edit.original_sha.lower()
            ):
                raise _replan_error(
                    "Source revision changed a file this plan edits after the plan was created; "
                    "re-plan before applying."
                )

    async def _record_committed_on_revision(self, plan: Plan, revision: str) -> Plan:
        """Persist the moved branch head the plan is committed on, separately from its base (D7)."""
        if plan.committed_on_revision != revision:
            recorder = getattr(self.plans, "set_plan_committed_on_revision", None)
            if callable(recorder):
                updated = await cast(Any, recorder)(plan.plan_id, revision, expected_digest=plan.digest)
                if updated is None:
                    raise PlanWorkerError(f"Plan {plan.plan_id!r} changed while recording its commit revision.")
                plan = updated
        return plan.model_copy(update={"committed_on_revision": revision})

    async def _planned_base_revision(self, plan: Plan) -> str | None:
        """Return the revision the plan was rendered on, even for the in-memory moved-head view (D7).

        ``_plan_on_committed_revision`` rewrites ``file_plan.base_revision`` to the moved head so the
        branch and commit are created on it; identities derived from the base (rollback plan IDs)
        and provenance must keep using the persisted planned base.
        """
        file_plan = plan.file_plan
        if file_plan is None:
            return None
        committed = plan.committed_on_revision
        if committed is None or committed.lower() != file_plan.base_revision.lower():
            return file_plan.base_revision
        persisted = await self.plans.get_plan(plan.plan_id)
        if persisted is None or persisted.digest != plan.digest or persisted.file_plan is None:
            return None
        return persisted.file_plan.base_revision

    async def _validate_current_binding(self, plan: Plan) -> None:
        if self.binding_reader is None:
            return
        source = plan.source
        try:
            binding = await self.binding_reader.get_workload_binding(
                source.cluster,
                source.namespace,
                source.workload_kind,
                source.workload_name,
            )
        except Exception as exc:
            raise PlanWorkerError(
                "Could not verify the current GitOps workload binding before applying the plan.",
                retryable=True,
            ) from exc
        if binding is None or binding.status is not BindingStatus.RESOLVED or binding.source is None:
            raise PlanWorkerError(
                "The GitOps workload binding is no longer resolved; refresh discovery and re-plan before applying.",
                plan_status=PlanStatus.STALE,
                action_status=ActionStatus.NONE,
            )

        expected_generation = source.evidence.get("binding_generation")
        if expected_generation is None and binding.generation > 0:
            changed = True
        else:
            changed = expected_generation is not None and expected_generation != binding.generation
        current_source = binding.source
        source_fields = (
            "id",
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
        if (
            changed
            or any(getattr(source, field) != getattr(current_source, field) for field in source_fields)
            or not (same_resolved_renderer_config(source, current_source))
        ):
            raise PlanWorkerError(
                "The GitOps workload binding changed after approval; re-plan against the current source "
                "before applying.",
                plan_status=PlanStatus.STALE,
                action_status=ActionStatus.NONE,
            )

    async def _ensure_pre_apply_baseline(self, execution: _Execution, plan: Plan) -> Plan:
        baseline = plan.pre_apply_baseline
        if baseline is None:
            if plan.status is not PlanStatus.APPROVED:
                raise PlanWorkerError(
                    "The plan has no pre-apply workload baseline; create and approve a fresh plan before retrying "
                    "so the baseline is captured before any Git mutation.",
                    retryable=True,
                )
            if self.workload_reader is None:
                raise PlanWorkerError(
                    "Could not capture the pre-apply workload baseline because no Kubernetes workload reader is "
                    "configured; verify worker configuration and Kubernetes read access, then retry.",
                    retryable=True,
                )
            await self._heartbeat(execution)
            try:
                snapshots = await self.workload_reader.read()
            except Exception as exc:
                raise PlanWorkerError(
                    "Could not capture the pre-apply workload baseline from Kubernetes; verify API availability "
                    "and worker read permissions, then retry.",
                    retryable=True,
                ) from exc
            try:
                baseline = self._capture_pre_apply_baseline(plan, snapshots)
            except PlanWorkerError:
                raise
            except Exception as exc:
                raise PlanWorkerError(
                    "Could not capture a valid pre-apply workload baseline because the workload response was "
                    "invalid; verify Kubernetes API health and retry.",
                    retryable=True,
                ) from exc
            await self._heartbeat(execution)
            try:
                persisted_plan = await self.plans.set_plan_pre_apply_baseline_if_absent(plan.plan_id, baseline)
            except Exception as exc:
                raise PlanWorkerError(
                    "Could not persist the immutable pre-apply workload baseline; verify plan storage and retry.",
                    retryable=True,
                ) from exc
            if persisted_plan is None:
                raise PlanWorkerError(
                    f"Plan {plan.plan_id!r} disappeared while saving its pre-apply workload baseline.",
                    retryable=True,
                )
            if not _same_plan_content(plan, persisted_plan):
                raise _replan_error("Persisted plan content changed while saving its pre-apply workload baseline.")
            plan = persisted_plan
            baseline = plan.pre_apply_baseline
            if baseline is None:
                raise PlanWorkerError(
                    "Plan storage did not retain the pre-apply workload baseline; verify storage and retry.",
                    retryable=True,
                )

        self._validate_pre_apply_baseline(plan, baseline)
        self._ensure_effective_change(plan, baseline)
        return plan

    @staticmethod
    def _ensure_effective_change(plan: Plan, baseline: PreApplyWorkloadBaseline) -> None:
        """Fail closed before any Git mutation when the cluster already runs every proposed request.

        A forward plan whose requests all equal the live requests, after unit
        normalization (``1Gi`` == ``1024Mi``, ``0.5`` == ``500m``), would open a
        pull request that changes nothing. The plan fails with the
        ``no_effective_change`` reason and no branch, commit or PR is created;
        apply detection then records the recommendation as applied, since the
        proposal is already live. Rollback plans restore the recorded pre-apply
        requests and are not checked here.
        """
        if plan.rollback_of_plan_id is not None:
            return
        proposed: list[tuple[int | None, int | None]] = []
        for operation in plan.action.operations:
            if not isinstance(operation, SetContainerResources):
                return
            live = baseline.container_requests.get(operation.container)
            if live is None:
                return
            requests = operation.resources.requests
            for resource, value in requests.items():
                parsed = _parse_request_quantity(resource, value)
                if parsed is None:
                    return
                proposed.append((parsed, live.cpu_millicores if resource == "cpu" else live.memory_bytes))
        if proposed and all(value == current for value, current in proposed):
            raise PlanWorkerError(
                "no_effective_change: the workload already runs every proposed request, so a pull request would "
                "change nothing; no branch or pull request was created.",
                plan_status=PlanStatus.FAILED,
                action_status=ActionStatus.FAILED,
                reason_code=NO_EFFECTIVE_CHANGE_REASON,
            )

    @staticmethod
    def _capture_pre_apply_baseline(
        plan: Plan,
        snapshots: Mapping[tuple[str, str, str], WorkloadSnapshot],
    ) -> PreApplyWorkloadBaseline:
        def unavailable(detail: str) -> PlanWorkerError:
            return PlanWorkerError(
                f"Could not capture a valid pre-apply workload baseline ({detail}); verify Kubernetes workload "
                "health and read access, then retry.",
                retryable=True,
            )

        if not isinstance(snapshots, Mapping):
            raise unavailable("the workload reader returned an invalid result")
        target = plan.action.target
        source_identity = (plan.source.namespace, plan.source.workload_kind, plan.source.workload_name)
        action_identity = (target.namespace, target.kind, target.name)
        if action_identity != source_identity:
            raise PlanWorkerError(
                "Plan action target does not match its resolved workload source; create and approve a corrected plan.",
                plan_status=PlanStatus.STALE,
                action_status=ActionStatus.NONE,
            )
        snapshot = snapshots.get(source_identity)
        if snapshot is None:
            raise unavailable(f"workload {source_identity!r} was not found")
        if (snapshot.namespace, snapshot.kind, snapshot.name) != source_identity:
            raise unavailable("the reader returned a snapshot for a different workload")
        if not isinstance(snapshot.uid, str) or not snapshot.uid.strip():
            raise unavailable("the workload UID is missing")
        if (
            isinstance(snapshot.generation, bool)
            or not isinstance(snapshot.generation, int)
            or snapshot.generation <= 0
        ):
            raise unavailable("the workload generation is missing or invalid")
        if (
            isinstance(snapshot.observed_generation, bool)
            or not isinstance(snapshot.observed_generation, int)
            or snapshot.observed_generation <= 0
        ):
            raise unavailable("the workload observedGeneration is missing or invalid")
        if snapshot.observed_generation < snapshot.generation:
            raise unavailable("the workload controller has not observed the current generation")
        if not getattr(snapshot, "container_request_values_valid", True):
            raise unavailable("one or more per-container resource requests are invalid")

        request_values = getattr(snapshot, "container_request_values", None)
        if request_values:
            raw_requests = request_values
        else:
            raw_requests = snapshot.container_requests
        if not isinstance(raw_requests, Mapping) or not raw_requests:
            raise unavailable("per-container resource requests are missing")

        containers: dict[str, ContainerRequestBaseline] = {}
        for name, requests in raw_requests.items():
            if not isinstance(name, str) or not name.strip() or name != name.strip():
                raise unavailable("a container name is missing or invalid")
            if not isinstance(requests, (tuple, list)) or len(requests) != 2:
                raise unavailable(f"resource requests for container {name!r} are invalid")
            cpu, memory = requests
            if any(
                value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0)
                for value in (cpu, memory)
            ):
                raise unavailable(f"resource requests for container {name!r} are invalid")
            try:
                containers[name] = ContainerRequestBaseline(cpu_millicores=cpu, memory_bytes=memory)
            except Exception:
                raise unavailable(f"resource requests for container {name!r} are invalid") from None

        required_containers = {
            operation.container for operation in plan.action.operations if isinstance(operation, SetContainerResources)
        }
        if not required_containers.issubset(containers):
            raise unavailable("the action target container is missing from the workload")
        try:
            return PreApplyWorkloadBaseline(
                workload_uid=snapshot.uid,
                generation=snapshot.generation,
                observed_generation=snapshot.observed_generation,
                container_requests=containers,
            )
        except Exception as exc:
            raise unavailable(str(exc)) from exc

    @staticmethod
    def _validate_pre_apply_baseline(plan: Plan, baseline: PreApplyWorkloadBaseline) -> None:
        if not isinstance(baseline, PreApplyWorkloadBaseline):
            raise PlanWorkerError(
                "The persisted pre-apply workload baseline is invalid; verify plan storage and retry.",
                retryable=True,
            )
        target = plan.action.target
        if (target.namespace, target.kind, target.name) != (
            plan.source.namespace,
            plan.source.workload_kind,
            plan.source.workload_name,
        ):
            raise PlanWorkerError(
                "Plan action target no longer matches its resolved workload source; "
                "create and approve a corrected plan.",
                plan_status=PlanStatus.STALE,
                action_status=ActionStatus.NONE,
            )
        if (
            not isinstance(baseline.workload_uid, str)
            or not baseline.workload_uid.strip()
            or isinstance(baseline.generation, bool)
            or not isinstance(baseline.generation, int)
            or baseline.generation <= 0
            or isinstance(baseline.observed_generation, bool)
            or not isinstance(baseline.observed_generation, int)
            or baseline.observed_generation < baseline.generation
            or not baseline.container_requests
        ):
            raise PlanWorkerError(
                "The persisted pre-apply workload baseline is invalid; verify plan storage and retry.",
                retryable=True,
            )
        required_containers = {
            operation.container for operation in plan.action.operations if isinstance(operation, SetContainerResources)
        }
        if not required_containers.issubset(baseline.container_requests):
            raise PlanWorkerError(
                "The persisted pre-apply workload baseline does not include every action container; "
                "create and approve a fresh plan before retrying.",
                retryable=True,
            )

    async def _recommendation_records_for_pr(
        self,
        plan: Plan,
    ) -> tuple[list[RecommendationRecord], list[int]]:
        if self.recommendation_repository is None:
            return [], list(plan.recommendation_ids)
        records: list[RecommendationRecord] = []
        missing_ids: list[int] = []
        for recommendation_id in plan.recommendation_ids:
            record = await self.recommendation_repository.get_recommendation_by_id(recommendation_id)
            if record is None or record.id != recommendation_id:
                missing_ids.append(recommendation_id)
            else:
                records.append(record)
        return records, missing_ids

    async def _validate_rollback_review(self, plan: Plan) -> None:
        rollback_of_plan_id = plan.rollback_of_plan_id
        if rollback_of_plan_id is None:
            if plan.plan_id.startswith("rbk-"):
                raise PlanWorkerError(
                    "Rollback plan is missing its persisted original-plan link.",
                    plan_status=PlanStatus.STALE,
                )
            return
        if self.recommendation_repository is None:
            raise PlanWorkerError(
                "Rollback recommendation state cannot be verified by the plan worker.",
                plan_status=PlanStatus.STALE,
            )
        original = await self.plans.get_plan(rollback_of_plan_id)
        planned_base_revision = await self._planned_base_revision(plan)
        if (
            original is None
            or original.plan_id != rollback_of_plan_id
            or original.status not in {PlanStatus.SYNCED, PlanStatus.APPLIED}
            or original.digest is None
            or original.file_plan is None
            or original.file_plan.digest != original.digest
            or original.pre_apply_baseline is None
            or plan.file_plan is None
            or set(original.recommendation_ids) != set(plan.recommendation_ids)
            or original.action != plan.action
            or not same_rollback_source(original.source, plan.source)
            or planned_base_revision is None
            or plan.plan_id != inverse_plan_id(original.plan_id, original.digest, planned_base_revision)
        ):
            raise PlanWorkerError(
                "Rollback link no longer matches a synced original plan.",
                plan_status=PlanStatus.STALE,
            )
        self._validate_pre_apply_baseline(original, original.pre_apply_baseline)
        operations = plan.action.operations
        if not operations or any(not isinstance(operation, SetContainerResources) for operation in operations):
            raise PlanWorkerError(
                "Rollback plan contains an unsupported resource operation.",
                plan_status=PlanStatus.STALE,
            )
        operation_containers = {
            operation.container for operation in operations if isinstance(operation, SetContainerResources)
        }
        recommendation_containers: set[str] = set()
        for recommendation_id in plan.recommendation_ids:
            recommendation = await self.recommendation_repository.get_recommendation_by_id(recommendation_id)
            if (
                recommendation is None
                or recommendation.id != recommendation_id
                or recommendation.status is not RecommendationStatus.ROLLBACK_REVIEW
                or recommendation.scope != "workload"
                or recommendation.action != plan.action
                or recommendation.namespace != plan.action.target.namespace
                or recommendation.owner_kind != plan.action.target.kind
                or recommendation.owner_name != plan.action.target.name
                or recommendation.container_name not in operation_containers
            ):
                raise PlanWorkerError(
                    "Every linked recommendation must remain in rollback_review before rollback application.",
                    plan_status=PlanStatus.STALE,
                )
            if recommendation.container_name is not None:
                recommendation_containers.add(recommendation.container_name)
        if recommendation_containers != operation_containers:
            raise PlanWorkerError(
                "Rollback plan does not cover every linked container recommendation.",
                plan_status=PlanStatus.STALE,
            )

    async def _apply_plan(
        self,
        execution: _Execution,
        plan: Plan,
        target: GitProviderTarget,
    ) -> _PlanResult:
        provider = target.provider
        await self._validate_rollback_review(plan)
        if not provider.supports("multi_file_commit"):
            raise PlanWorkerError(
                f"Git provider {provider.name!r} cannot guarantee an atomic multi-file commit.",
                plan_status=PlanStatus.FAILED,
                action_status=ActionStatus.FAILED,
            )

        branch = plan_branch_name(plan)
        await self._ensure_pending_pull_request_records(plan, target, branch)
        existing_pr = await self._git_call(
            execution,
            provider.find_pull_request,
            target.repository,
            branch,
            plan.source.ref,
        )
        if existing_pr is not None:
            self._validate_pull_request(existing_pr, branch, plan.source.ref)
            branch_state = await self._branch_state(execution, target, branch, plan)
            if branch_state != "applied":
                raise _replan_error("Existing pull request branch does not match the approved file plan.")
            context = await self._pull_request_context(plan, target, branch)
            metadata = self._pull_request_metadata(context)
            await self._git_call(
                execution,
                provider.ensure_pull_request_options,
                target.repository,
                existing_pr,
                self._existing_pull_request_options(provider, existing_pr, metadata),
            )
            await self._validate_rollback_review(plan)
            await self._record_open_pull_request(plan, target, branch, existing_pr)
            await self._decorate_pull_request(execution, context, target, existing_pr, metadata)
            return _PlanResult(target, branch, _pull_request_head_sha(existing_pr), existing_pr)

        if plan.status is PlanStatus.PR_OPEN:
            raise _replan_error("Plan is marked PR-open but no matching open pull request can be found.")

        try:
            await self._git_call(
                execution,
                provider.create_branch,
                target.repository,
                branch,
                plan.source.ref,
            )
        except GitProviderError as exc:
            if isinstance(exc, GitProviderTransientError):
                raise
            branch_state = await self._branch_state(execution, target, branch, plan)
            if branch_state not in {"base", "applied"}:
                raise _replan_error(
                    f"Could not safely reuse deterministic plan branch after provider rejected branch creation: {exc}"
                ) from exc

        branch_state = await self._branch_state(execution, target, branch, plan)
        commit_sha: str | None = None
        if branch_state == "base":
            await self._validate_rollback_review(plan)
            commit_context = await self._pull_request_context(plan, target, branch)
            request = self._commit_request(
                plan,
                target,
                branch,
                message=render_commit_message(commit_context, self.render_settings),
            )
            try:
                result = await self._git_call(execution, provider.create_commit, request)
            except GitConflictError:
                branch_state = await self._branch_state(execution, target, branch, plan)
                if branch_state != "applied":
                    raise _replan_error("Plan branch moved during atomic commit; re-plan before applying.")
            else:
                self._validate_commit_result(result, plan, branch)
                commit_sha = result.commit_sha
                branch_state = await self._branch_state(execution, target, branch, plan)
                if branch_state != "applied":
                    raise _replan_error("Provider commit did not leave the branch in the exact planned state.")
        elif branch_state != "applied":
            raise _replan_error("Deterministic plan branch contains unverified changes; re-plan before applying.")

        # No second pull-request lookup here: the lookup above ran under this
        # lease, and a request opened meanwhile makes creation fail, which falls
        # back to adopting the matching open request below (D5).
        await self._heartbeat(execution, force=True)
        await self._validate_rollback_review(plan)
        context = await self._pull_request_context(plan, target, branch)
        metadata = self._pull_request_metadata(context)
        content = render_pull_request(context, self.render_settings)
        title = content.title
        if (
            metadata.draft
            and not provider.supports(GitProviderCapability.DRAFT_PULL_REQUEST)
            and provider.name == "gitea"
        ):
            # Gitea has no draft API; a "WIP:" prefix marks the request as work in progress (J2).
            title = f"WIP: {title}"
        creation_options = self._creation_pull_request_options(provider, metadata)
        try:
            pull_request = await provider.create_pull_request(
                target.repository,
                head=branch,
                base=plan.source.ref,
                title=title,
                body=content.body,
                options=creation_options,
            )
        except GitProviderTransientError:
            raise
        except GitProviderError as exc:
            existing_pr = await self._git_call(
                execution,
                provider.find_pull_request,
                target.repository,
                branch,
                plan.source.ref,
            )
            if existing_pr is not None:
                pull_request = existing_pr
                await self._git_call(
                    execution,
                    provider.ensure_pull_request_options,
                    target.repository,
                    pull_request,
                    self._existing_pull_request_options(provider, pull_request, metadata),
                )
            elif creation_options != self.pull_request_options:
                # The provider refused the draft GreenKube added for a high-risk plan (e.g. a
                # GitHub plan without draft pull requests): open the request ready for review.
                logger.warning("Git provider refused a draft pull request for plan %s; opening it ready.", plan.plan_id)
                try:
                    pull_request = await provider.create_pull_request(
                        target.repository,
                        head=branch,
                        base=plan.source.ref,
                        title=content.title,
                        body=content.body,
                        options=self.pull_request_options,
                    )
                except GitProviderTransientError:
                    raise
                except GitProviderError as retry_error:
                    raise PlanWorkerError(
                        f"Provider rejected pull-request creation without a matching open request: {retry_error}",
                        retryable=True,
                    ) from retry_error
            else:
                raise PlanWorkerError(
                    f"Provider rejected pull-request creation without a matching open request: {exc}",
                    retryable=True,
                ) from exc
        self._validate_pull_request(pull_request, branch, plan.source.ref)
        pull_request_head = _pull_request_head_sha(pull_request)
        if commit_sha is None or pull_request_head is None or pull_request_head.lower() != commit_sha.lower():
            # Only re-read the branch when the request head is not the commit
            # this operation created and already verified.
            branch_state = await self._branch_state(execution, target, branch, plan)
            if branch_state != "applied":
                raise _replan_error("Pull-request branch changed while it was being opened; re-plan before applying.")
        commit_sha = pull_request_head or commit_sha
        await self._record_open_pull_request(plan, target, branch, pull_request, commit_sha=commit_sha)
        await self._decorate_pull_request(execution, context, target, pull_request, metadata, head_sha=commit_sha)
        return _PlanResult(target, branch, commit_sha, pull_request)

    # ------------------------------------------------------------------
    # Pull-request content and metadata (roadmap J1, J2, J3, J5)
    # ------------------------------------------------------------------

    @property
    def render_settings(self) -> PullRequestRenderSettings:
        if self._render_settings is None:
            self._render_settings = PullRequestRenderSettings.from_config()
        return self._render_settings

    @property
    def pull_request_features(self) -> PullRequestFeatures:
        if self._pull_request_features is None:
            self._pull_request_features = PullRequestFeatures.from_config()
        return self._pull_request_features

    async def _pull_request_context(self, plan: Plan, target: GitProviderTarget, branch: str) -> PullRequestContext:
        recommendations, missing_recommendation_ids = await self._recommendation_records_for_pr(plan)
        return PullRequestContext(
            plan=plan,
            repository=target.repository.full_name,
            branch=branch,
            recommendations=tuple(recommendations),
            missing_recommendation_ids=tuple(missing_recommendation_ids),
            planned_base_revision=await self._planned_base_revision(plan),
        )

    def _pull_request_metadata(self, context: PullRequestContext) -> PullRequestMetadata:
        features = self.pull_request_features
        return derive_metadata(
            context,
            default_labels=features.default_labels,
            draft_high_risk=features.draft_high_risk,
        )

    def _creation_pull_request_options(
        self,
        provider: PlanGitProvider,
        metadata: PullRequestMetadata,
    ) -> PullRequestOptions:
        """Configured options, plus draft for a high-risk plan when the provider has drafts (J2)."""
        options = self.pull_request_options
        if metadata.draft and not options.draft and provider.supports(GitProviderCapability.DRAFT_PULL_REQUEST):
            return options.model_copy(update={"draft": True})
        return options

    def _existing_pull_request_options(
        self,
        provider: PlanGitProvider,
        pull_request: PullRequestInfo,
        metadata: PullRequestMetadata,
    ) -> PullRequestOptions:
        """Options re-applied to an adopted request: never undraft a request GreenKube opened as draft."""
        options = self.pull_request_options
        raw = pull_request.raw if isinstance(pull_request.raw, dict) else {}
        if (
            metadata.draft
            and not options.draft
            and raw.get("draft") is True
            and provider.supports(GitProviderCapability.DRAFT_PULL_REQUEST)
        ):
            return options.model_copy(update={"draft": True})
        return options

    async def _decorate_pull_request(
        self,
        execution: _Execution,
        context: PullRequestContext,
        target: GitProviderTarget,
        pull_request: PullRequestInfo,
        metadata: PullRequestMetadata,
        *,
        head_sha: str | None = None,
    ) -> None:
        """Best effort: derived labels, CODEOWNERS reviewers and the validation status.

        The request is already open and recorded, so a provider refusal here is
        logged and never fails or retries the plan operation.
        """
        provider: Any = target.provider
        features = self.pull_request_features
        repository = target.repository
        plan = context.plan
        if provider.supports(GitProviderCapability.PULL_REQUEST_METADATA):
            reviewers: list[str] = []
            team_reviewers: list[str] = []
            if features.codeowners and provider.supports(GitProviderCapability.REVIEWERS):
                try:
                    reviewers, team_reviewers = await self._codeowner_reviewers(
                        execution, provider, context, repository
                    )
                except PlanOperationLeaseError:
                    raise
                except Exception as error:
                    logger.warning("Could not read CODEOWNERS for plan %s (%s).", plan.plan_id, _safe_error(error))
                if not provider.supports(GitProviderCapability.TEAM_REVIEWERS):
                    team_reviewers = []
            labels = list(metadata.labels) if provider.supports(GitProviderCapability.LABELS) else []
            if labels or reviewers or team_reviewers:
                try:
                    await self._git_call(
                        execution,
                        partial(
                            provider.add_pull_request_metadata,
                            repository,
                            pull_request,
                            labels=labels,
                            reviewers=reviewers,
                            team_reviewers=team_reviewers,
                        ),
                    )
                except PlanOperationLeaseError:
                    raise
                except Exception as error:
                    logger.warning(
                        "Could not add labels or reviewers to the pull request of plan %s (%s).",
                        plan.plan_id,
                        _safe_error(error),
                    )
        sha = head_sha or _pull_request_head_sha(pull_request)
        if features.commit_status and sha and provider.supports(GitProviderCapability.COMMIT_STATUS):
            state, description = _validation_status(plan)
            try:
                await self._git_call(
                    execution,
                    partial(
                        provider.set_commit_status,
                        repository,
                        sha,
                        state=state,
                        context=VALIDATION_STATUS_CONTEXT,
                        description=description,
                        target_url=self.render_settings.link(f"changes/{plan.plan_id}"),
                    ),
                )
            except PlanOperationLeaseError:
                raise
            except Exception as error:
                logger.warning(
                    "Could not publish the %s status for plan %s (%s).",
                    VALIDATION_STATUS_CONTEXT,
                    plan.plan_id,
                    _safe_error(error),
                )

    async def _codeowner_reviewers(
        self,
        execution: _Execution,
        provider: Any,
        context: PullRequestContext,
        repository: GitRepository,
    ) -> tuple[list[str], list[str]]:
        """Reviewers owning the touched paths in CODEOWNERS at the planned base revision (J3)."""
        file_plan = context.plan.file_plan
        revision = context.base_revision
        if file_plan is None or not GIT_OBJECT_SHA.fullmatch(revision or ""):
            return [], []
        codeowners = await self._git_call(execution, provider.read_codeowners, repository, revision)
        if codeowners is None:
            return [], []
        owners = codeowners.owners_for(edit.path for edit in file_plan.files)
        if not owners:
            return [], []
        users, teams = await self._git_call(execution, provider.codeowner_reviewers, repository, owners)
        configured_users = {name.casefold() for name in self.pull_request_options.reviewers}
        configured_teams = {name.casefold() for name in self.pull_request_options.team_reviewers}
        return (
            [name for name in users if name.casefold() not in configured_users],
            [name for name in teams if name.casefold() not in configured_teams],
        )

    def _validate_operation_and_plan(self, operation: Operation, plan: Plan, now: datetime) -> None:
        if operation.plan_id != plan.plan_id:
            raise PlanWorkerError("Claimed operation does not match the loaded plan.")
        if plan.digest is None or not SHA256_DIGEST.fullmatch(plan.digest):
            raise PlanWorkerError(
                "Plan has no valid persisted digest.",
                plan_status=PlanStatus.FAILED,
                action_status=ActionStatus.FAILED,
            )
        if operation.idempotency_key != f"plan:{plan.plan_id}:{plan.digest}":
            raise PlanWorkerError(
                "Claimed operation digest does not match the persisted plan digest.",
                plan_status=PlanStatus.STALE,
                action_status=ActionStatus.NONE,
            )
        if operation.provider_id is not None and operation.provider_id != plan.source.provider_id:
            raise PlanWorkerError("Claimed operation provider does not match its plan.")
        if plan.file_plan is None or plan.file_plan.digest != plan.digest:
            raise PlanWorkerError(
                "Plan file digest does not match its persisted digest.",
                plan_status=PlanStatus.STALE,
                action_status=ActionStatus.NONE,
            )
        if not plan.file_plan.validation.passed:
            raise PlanWorkerError(
                "Plan render validation did not pass.",
                plan_status=PlanStatus.FAILED,
                action_status=ActionStatus.FAILED,
            )
        if not GIT_OBJECT_SHA.fullmatch(plan.file_plan.base_revision):
            raise PlanWorkerError(
                "Plan base revision is not an immutable full Git commit SHA.",
                plan_status=PlanStatus.STALE,
                action_status=ActionStatus.NONE,
            )
        if plan.status not in _ACTIVE_PLAN_STATUSES:
            raise PlanWorkerError(f"Plan cannot be applied from status {plan.status.value}.")
        if plan.expires_at is not None and ensure_utc(plan.expires_at) <= now:
            raise PlanWorkerError(
                "Plan has expired; create and approve a new plan.",
                plan_status=PlanStatus.STALE,
                action_status=ActionStatus.NONE,
            )
        if plan.source.status is not BindingStatus.RESOLVED:
            raise PlanWorkerError(
                "Plan source is no longer resolved; re-plan after resolving the workload binding.",
                plan_status=PlanStatus.STALE,
                action_status=ActionStatus.NONE,
            )
        if plan.source.expires_at is not None and ensure_utc(plan.source.expires_at) <= now:
            raise PlanWorkerError(
                "Plan source binding has expired; refresh the binding and re-plan.",
                plan_status=PlanStatus.STALE,
                action_status=ActionStatus.NONE,
            )

    @staticmethod
    def _validate_target(source: WorkloadSource, target: GitProviderTarget) -> None:
        provider_name = target.provider.name.strip().lower()
        if provider_name not in _SUPPORTED_PROVIDER_TYPES:
            raise PlanWorkerError(f"Resolved Git provider {provider_name!r} is not supported.")
        try:
            expected_owner, expected_name = parse_repo_url(source.repo_url, provider=provider_name)
        except GitProviderError:
            raise PlanWorkerError("Plan source repository is invalid.") from None
        repository = target.repository
        if (
            repository.provider != provider_name
            or repository.owner != expected_owner
            or repository.name != expected_name
            or repository.default_branch != source.ref
        ):
            raise PlanWorkerError(
                "Resolved Git provider target does not match the plan source repository and base ref."
            )

    @staticmethod
    def _validate_commit_result(result: GitCommitResult, plan: Plan, branch: str) -> None:
        file_plan = plan.file_plan
        assert file_plan is not None
        expected_paths = [edit.path for edit in file_plan.files]
        if (
            result.branch != branch
            or result.files_changed != expected_paths
            or not isinstance(result.commit_sha, str)
            or not GIT_OBJECT_SHA.fullmatch(result.commit_sha)
        ):
            raise PlanWorkerError(
                "Git provider returned a commit result that does not match the atomic file plan.",
                plan_status=PlanStatus.FAILED,
                action_status=ActionStatus.FAILED,
            )

    def _commit_request(
        self,
        plan: Plan,
        target: GitProviderTarget,
        branch: str,
        *,
        start_revision: str | None = None,
        message: str | None = None,
    ) -> GitCommitRequest:
        """Build the atomic commit; ``start_revision`` defaults to the plan's base revision.

        ``message`` is the rendered commit message with its ``GreenKube-Plan`` and
        ``GreenKube-Recommendations`` trailers (J1).
        """
        file_plan = plan.file_plan
        assert file_plan is not None
        if plan.digest is None:
            raise PlanWorkerError("Cannot create a commit without a plan digest.")
        files = [GitFileEdit(path=edit.path, action=edit.operation, content=edit.content) for edit in file_plan.files]
        if message is None:
            message = (
                f"Rollback GreenKube plan {plan.rollback_of_plan_id} ({plan.digest[:19]})"
                if plan.rollback_of_plan_id is not None
                else f"Apply GreenKube plan {plan.plan_id} ({plan.digest[:19]})"
            )
        return GitCommitRequest(
            repo=target.repository,
            branch=branch,
            base_revision=start_revision or file_plan.base_revision,
            message=message,
            files=files,
            author_name=target.provider.author_name,
            author_email=target.provider.author_email,
        )

    async def _branch_state(
        self,
        execution: _Execution,
        target: GitProviderTarget,
        branch: str,
        plan: Plan,
        *,
        start_revision: str | None = None,
    ) -> str:
        """Classify the plan branch as ``base``, ``applied`` or ``mismatch``.

        The branch is compared with the immutable start revision (the plan's
        base revision unless a caller commits on a newer one) through one tree
        comparison plus reads of the planned paths only, so the cost does not
        grow with repository size (D5, BUG-47).
        """
        file_plan = plan.file_plan
        assert file_plan is not None
        start = start_revision or file_plan.base_revision
        provider = target.provider
        repository = target.repository
        planned_paths = [edit.path for edit in file_plan.files]

        start_blobs = await self._git_call(execution, provider.get_blob_ids, repository, start, planned_paths)
        if not isinstance(start_blobs, Mapping) or not set(start_blobs) <= set(planned_paths):
            raise _replan_error(f"Provider returned invalid blob IDs for revision {start!r}.")
        expected = _expected_changes(start_blobs, file_plan.files, start)

        try:
            comparison = await self._git_call(execution, provider.compare_trees, repository, start, branch)
        except GitProviderError as exc:
            if isinstance(exc, GitProviderTransientError) or exc.status_code != 404:
                raise
            # The start revision was just read, so the plan branch is missing
            # (deleted, or never created): it does not hold the planned state.
            return "mismatch"
        changes = _validated_tree_changes(comparison, start, branch)
        if not changes:
            return "base"
        if changes.keys() != expected.keys():
            return "mismatch"

        unresolved: list[str] = []
        for path, expected_blob in expected.items():
            change = changes[path]
            if expected_blob is None:
                if change.status != "deleted":
                    return "mismatch"
            elif change.status == "deleted":
                return "mismatch"
            elif change.blob_sha is None or change.blob_sha.lower() != expected_blob:
                unresolved.append(path)
        if not unresolved:
            return "applied"

        # The comparison listed paths without (matching) blob IDs: read only
        # the planned paths at the compared head, then fall back to content.
        head = comparison.head_revision or branch
        head_blobs = await self._git_call(execution, provider.get_blob_ids, repository, head, unresolved)
        if not isinstance(head_blobs, Mapping):
            raise _replan_error(f"Provider returned invalid blob IDs for ref {head!r}.")
        planned_content = {edit.path: edit.content for edit in file_plan.files}
        for path in unresolved:
            blob = head_blobs.get(path)
            if blob is None:
                return "mismatch"
            if isinstance(blob, str) and blob.lower() == expected[path]:
                continue
            git_file = await self._git_call(execution, provider.get_file, repository, path, head)
            if git_file is None or git_file.path != path or git_file.content != planned_content[path]:
                return "mismatch"
        return "applied"

    async def _ensure_pending_pull_request_records(
        self,
        plan: Plan,
        target: GitProviderTarget,
        branch: str,
    ) -> None:
        for recommendation_id in plan.recommendation_ids:
            attempt = await self._find_record_for_branch(
                recommendation_id,
                target.provider.name,
                target.repository.full_name,
                plan.source.ref,
                branch,
            )
            if attempt is None:
                await self.pull_request_repository.save_pull_request(
                    PullRequestRecord(
                        recommendation_id=recommendation_id,
                        provider=target.provider.name,
                        provider_id=plan.source.provider_id,
                        plan_id=plan.plan_id,
                        repo=target.repository.full_name,
                        base_branch=plan.source.ref,
                        head_branch=branch,
                        status=PullRequestStatus.PENDING,
                    )
                )
                continue
            if attempt.pr_number is not None or _pull_request_status(attempt.status) in {
                PullRequestStatus.OPEN.value,
                PullRequestStatus.MERGED.value,
                PullRequestStatus.CLOSED.value,
            }:
                raise _replan_error(
                    "A previous pull-request record exists for the deterministic plan branch; "
                    "re-plan to avoid a duplicate."
                )
            if (
                _pull_request_status(attempt.status) != PullRequestStatus.PENDING.value
                or attempt.error is not None
                or attempt.plan_id != plan.plan_id
                or attempt.provider_id != plan.source.provider_id
            ):
                if attempt.id is None:
                    raise PlanWorkerError("Stored pull-request attempt is missing its database ID.")
                expected_status = _pull_request_status(attempt.status)
                if expected_status is None:
                    raise PlanWorkerError("Stored pull-request attempt has an invalid state.")
                await self.pull_request_repository.update_pull_request(
                    attempt.id,
                    {
                        "status": PullRequestStatus.PENDING,
                        "error": None,
                        "plan_id": plan.plan_id,
                        "provider_id": plan.source.provider_id,
                    },
                    expected_statuses=(expected_status,),
                    expected_plan_id=attempt.plan_id if isinstance(attempt.plan_id, str) else None,
                )

    async def _record_open_pull_request(
        self,
        plan: Plan,
        target: GitProviderTarget,
        branch: str,
        pull_request: PullRequestInfo,
        *,
        commit_sha: str | None = None,
    ) -> None:
        commit_sha = _pull_request_head_sha(pull_request) or commit_sha
        for recommendation_id in plan.recommendation_ids:
            attempt = await self._find_record_for_branch(
                recommendation_id,
                target.provider.name,
                target.repository.full_name,
                plan.source.ref,
                branch,
            )
            if attempt is None:
                await self.pull_request_repository.save_pull_request(
                    PullRequestRecord(
                        recommendation_id=recommendation_id,
                        provider=target.provider.name,
                        provider_id=plan.source.provider_id,
                        plan_id=plan.plan_id,
                        commit_sha=commit_sha,
                        repo=target.repository.full_name,
                        base_branch=plan.source.ref,
                        head_branch=branch,
                        pr_number=pull_request.number,
                        pr_url=pull_request.url,
                        status=PullRequestStatus.OPEN,
                    )
                )
                continue
            if attempt.pr_number not in (None, pull_request.number):
                raise _replan_error("Stored pull-request record points to a different provider request.")
            if attempt.id is None:
                raise PlanWorkerError("Stored pull-request attempt is missing its database ID.")
            expected_status = _pull_request_status(attempt.status)
            if expected_status is None:
                raise PlanWorkerError("Stored pull-request attempt has an invalid state.")
            await self.pull_request_repository.update_pull_request(
                attempt.id,
                {
                    "pr_number": pull_request.number,
                    "pr_url": pull_request.url,
                    "status": PullRequestStatus.OPEN,
                    "error": None,
                    "provider_id": plan.source.provider_id,
                    "plan_id": plan.plan_id,
                    "commit_sha": commit_sha,
                },
                expected_statuses=(expected_status,),
                expected_plan_id=attempt.plan_id if isinstance(attempt.plan_id, str) else None,
            )

    async def _find_record_for_branch(
        self,
        recommendation_id: int,
        provider: str,
        repo: str,
        base_branch: str,
        head_branch: str,
    ) -> PullRequestRecord | None:
        records = await self.pull_request_repository.get_pull_requests_for_recommendation(recommendation_id)
        matches = [
            record
            for record in records
            if record.provider == provider
            and record.repo == repo
            and record.base_branch == base_branch
            and record.head_branch == head_branch
        ]
        if len(matches) > 1:
            raise _replan_error("Multiple pull-request records refer to the same deterministic plan branch.")
        return matches[0] if matches else None

    @staticmethod
    def _validate_pull_request(pull_request: PullRequestInfo, branch: str, base: str) -> None:
        if pull_request.merged or pull_request.status.strip().lower() not in _OPEN_PULL_REQUEST_STATES:
            raise _replan_error("Provider lookup returned a non-open pull request for the plan branch.")
        raw = pull_request.raw if isinstance(pull_request.raw, dict) else {}
        head = raw.get("head")
        if isinstance(head, dict) and isinstance(head.get("ref"), str) and head["ref"] != branch:
            raise _replan_error("Provider pull request head does not match the deterministic plan branch.")
        base_data = raw.get("base")
        if isinstance(base_data, dict) and isinstance(base_data.get("ref"), str) and base_data["ref"] != base:
            raise _replan_error("Provider pull request base does not match the plan source ref.")

    async def _git_call(
        self,
        execution: _Execution,
        method: Callable[..., Awaitable[_RESULT]],
        *args,
    ) -> _RESULT:
        await self._heartbeat(execution)
        return await method(*args)

    async def _heartbeat(self, execution: _Execution, *, force: bool = False) -> None:
        now = ensure_utc(self.clock())
        record_automation_worker_heartbeat(now.timestamp())
        refresh_margin = timedelta(seconds=max(1, self.lease_seconds // 3))
        if force or execution.lease.lease_expires_at <= now + refresh_margin:
            execution.lease = await self.operations.heartbeat(execution.lease, lease_seconds=self.lease_seconds)

    async def _adopt_open_pull_request(self, execution: _Execution, plan: Plan) -> _PlanResult | None:
        """Adopt the open PR of a reclaimed operation before any staleness check (D10, BUG-52).

        A worker can lose its lease after the PR was opened but before the operation was
        marked succeeded. The reclaiming worker must track that PR (``PR_OPEN``) instead of
        marking the plan stale because the binding, TTL or branch head moved meanwhile.
        """
        operation = execution.lease.operation
        file_plan = plan.file_plan
        if (
            plan.status not in _RECLAIMABLE_PLAN_STATUSES
            or operation.plan_id != plan.plan_id
            or plan.digest is None
            or operation.idempotency_key != f"plan:{plan.plan_id}:{plan.digest}"
            or file_plan is None
            or file_plan.digest != plan.digest
        ):
            return None
        target = await self.provider_resolver.resolve(plan.source)
        try:
            self._validate_target(plan.source, target)
            branch = plan_branch_name(plan)
            pull_request = await self._git_call(
                execution,
                target.provider.find_pull_request,
                target.repository,
                branch,
                plan.source.ref,
            )
            if pull_request is None:
                return None
            try:
                self._validate_pull_request(pull_request, branch, plan.source.ref)
                branch_state = await self._branch_state(execution, target, branch, _plan_on_committed_revision(plan))
            except PlanWorkerError:
                return None
            if branch_state != "applied":
                return None
            context = await self._pull_request_context(plan, target, branch)
            metadata = self._pull_request_metadata(context)
            await self._git_call(
                execution,
                target.provider.ensure_pull_request_options,
                target.repository,
                pull_request,
                self._existing_pull_request_options(target.provider, pull_request, metadata),
            )
            await self._record_open_pull_request(plan, target, branch, pull_request)
            await self._decorate_pull_request(execution, context, target, pull_request, metadata)
            return _PlanResult(target, branch, _pull_request_head_sha(pull_request), pull_request)
        finally:
            try:
                await target.provider.close()
            except Exception:
                pass

    async def _finish_success(self, execution: _Execution, plan: Plan, result: _PlanResult) -> None:
        await self._heartbeat(execution, force=True)
        if plan.status is not PlanStatus.PR_OPEN:
            updated = await self.plans.set_plan_status(
                plan.plan_id,
                PlanStatus.PR_OPEN,
                expected_statuses=(PlanStatus.APPLYING,),
                expected_digest=plan.digest,
                actor="automation-worker",
            )
            if updated is None:
                raise PlanWorkerError(f"Plan {plan.plan_id!r} changed after its pull request was opened.")
        await self.plans.set_recommendation_action_status(
            plan.recommendation_ids,
            ActionStatus.PR_OPEN,
            expected_statuses=(ActionStatus.PLAN_READY, ActionStatus.PR_OPEN),
            actor="automation-worker",
            plan_id=plan.plan_id,
        )
        await self._heartbeat(execution, force=True)
        completed_operation = await self.operations.mark_succeeded(
            execution.lease,
            provider_id=plan.source.provider_id,
            commit_sha=result.commit_sha,
            pull_request_number=result.pull_request.number,
        )
        completed_status = getattr(completed_operation, "status", None)
        completed_status = getattr(completed_status, "value", completed_status)
        if isinstance(completed_status, str):
            record_plan_operation(completed_status, _plan_operation_type(plan))
        # The provider authenticated and wrote a branch, a commit and a pull request.
        await self._record_provider_check(plan.source.provider_id, "verified")

    async def _record_provider_check(self, provider_id: str, status: str, *, error_code: str | None = None) -> None:
        """Best effort: a provider state write never fails or retries the operation."""
        record = getattr(self.provider_resolver, "record_check", None)
        if record is None:
            return
        try:
            await record(provider_id, status, error_code=error_code)
        except Exception as exc:
            logger.warning("Could not record Git provider %s state %s: %s", provider_id, status, exc)

    async def _finish_failure(self, execution: _Execution, plan: Plan | None, error: Exception) -> None:
        retryable = _is_retryable(error)
        plan_status = error.plan_status if isinstance(error, PlanWorkerError) else None
        action_status = error.action_status if isinstance(error, PlanWorkerError) else None
        if plan is not None:
            try:
                await self._heartbeat(execution, force=True)
                persisted_plan = await self.plans.get_plan(plan.plan_id)
                if persisted_plan is None:
                    plan = None
                else:
                    plan = persisted_plan
            except PlanOperationLeaseError:
                raise
            except Exception:
                retryable = True
                error = PlanWorkerError(
                    "Could not refresh plan state while recording failure.",
                    retryable=True,
                )
                plan_status = None
                action_status = None

        if plan is not None:
            active_status = plan.status in _ACTIVE_PLAN_STATUSES
            if not retryable and plan_status is None and active_status:
                plan_status = PlanStatus.FAILED
                action_status = ActionStatus.FAILED
            if plan_status is not None and (active_status or plan.status is plan_status):
                try:
                    await self._heartbeat(execution, force=True)
                    updated = await self.plans.set_plan_status(
                        plan.plan_id,
                        plan_status,
                        expected_statuses=(plan.status,),
                        expected_digest=plan.digest,
                        actor="automation-worker",
                        reason_code=(
                            error.reason_code
                            if isinstance(error, PlanWorkerError) and error.reason_code
                            else "worker_failure"
                        ),
                    )
                    if updated is None:
                        raise PlanWorkerError(f"Plan {plan.plan_id!r} changed while recording failure.")
                    if action_status is not None:
                        expected_action_statuses = (
                            (ActionStatus.PLAN_READY,)
                            if action_status is ActionStatus.NONE
                            else (ActionStatus.PLAN_READY, ActionStatus.PR_OPEN, action_status)
                        )
                        await self.plans.set_recommendation_action_status(
                            plan.recommendation_ids,
                            action_status,
                            expected_statuses=expected_action_statuses,
                            actor="automation-worker",
                            plan_id=plan.plan_id,
                        )
                except PlanOperationLeaseError:
                    raise
                except Exception:
                    retryable = True
                    error = PlanWorkerError(
                        "Could not persist plan failure state.",
                        retryable=True,
                    )

        provider_error_code = _provider_access_error_code(error)
        if plan is not None and provider_error_code is not None:
            await self._record_provider_check(plan.source.provider_id, "error", error_code=provider_error_code)
        failed_operation = await self.operations.mark_failed(
            execution.lease,
            _error_message(error),
            retryable=retryable,
        )
        failed_status = getattr(failed_operation.status, "value", failed_operation.status)
        record_plan_operation(
            failed_status if isinstance(failed_status, str) else "unknown",
            _plan_operation_type(plan),
        )
        record_automation_failure("worker")
        if retryable and failed_operation.status is OperationStatus.FAILED and plan is not None:
            current = await self.plans.get_plan(plan.plan_id)
            if current is not None and current.status is PlanStatus.PR_OPEN:
                await self.plans.set_recommendation_action_status(
                    plan.recommendation_ids,
                    ActionStatus.PR_OPEN,
                    expected_statuses=(ActionStatus.PLAN_READY, ActionStatus.PR_OPEN),
                    actor="automation-worker",
                    plan_id=plan.plan_id,
                )
            elif current is not None and current.status in {PlanStatus.APPROVED, PlanStatus.APPLYING}:
                failed = await self.plans.set_plan_status(
                    plan.plan_id,
                    PlanStatus.FAILED,
                    expected_statuses=(current.status,),
                    expected_digest=current.digest,
                    actor="automation-worker",
                    reason_code="operation_retry_exhausted",
                )
                if failed is not None:
                    await self.plans.set_recommendation_action_status(
                        plan.recommendation_ids,
                        ActionStatus.FAILED,
                        expected_statuses=(ActionStatus.PLAN_READY,),
                        actor="automation-worker",
                        plan_id=plan.plan_id,
                    )


def _provider_access_error_code(error: BaseException) -> str | None:
    """``auth_failed`` or ``permission_denied`` when the provider refused the token, else None."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, GitProviderError) and not isinstance(current, GitProviderTransientError):
            if current.status_code == 401:
                return "auth_failed"
            if current.status_code == 403:
                return "permission_denied"
        current = current.__cause__ or current.__context__
    return None


def _parse_request_quantity(resource: str, value: object) -> int | None:
    """Normalize a proposed request to millicores or bytes; None when it cannot be compared."""
    from greenkube.utils.k8s_quantities import parse_cpu_quantity, parse_memory_quantity

    parser = {"cpu": parse_cpu_quantity, "memory": parse_memory_quantity}.get(resource)
    if parser is None or not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        parsed = parser(value)
    except Exception:
        return None
    return parsed if isinstance(parsed, int) and not isinstance(parsed, bool) else None


def _plan_on_committed_revision(plan: Plan) -> Plan:
    """In-memory view of a plan applied on its recorded moved head instead of its base revision.

    The touched files are byte-identical at both revisions, so the file edits and digest are
    unchanged; only the branch/commit parent moves. Never persisted.
    """
    file_plan = plan.file_plan
    revision = plan.committed_on_revision
    if file_plan is None or revision is None or revision.lower() == file_plan.base_revision.lower():
        return plan
    return plan.model_copy(update={"file_plan": file_plan.model_copy(update={"base_revision": revision})})


def plan_branch_name(plan: Plan) -> str:
    """Return a stable, collision-resistant branch for one plan digest."""
    if plan.digest is None:
        raise ValueError("a deterministic plan branch requires a plan digest")
    branch_key = hashlib.sha256(f"{plan.plan_id}:{plan.digest}".encode()).hexdigest()[:32]
    return f"greenkube/plan/{branch_key}"


def _same_plan_content(left: Plan, right: Plan) -> bool:
    return (
        left.plan_id == right.plan_id
        and left.recommendation_ids == right.recommendation_ids
        and left.source == right.source
        and left.action == right.action
        and left.file_plan == right.file_plan
        and left.digest == right.digest
        and left.change_set_id == right.change_set_id
        and left.rollback_of_plan_id == right.rollback_of_plan_id
        and left.created_at == right.created_at
        and left.expires_at == right.expires_at
    )


def _expected_changes(
    start_blobs: Mapping[str, str | None],
    edits: Sequence[FileEdit],
    start_revision: str,
) -> dict[str, str | None]:
    """Validate the start revision against the plan and return the expected tree delta.

    Maps every path the plan really changes to its expected blob ID (``None``
    for deletions). Updates that rewrite identical bytes change nothing.
    """
    object_format = "sha256" if len(start_revision) == 64 else "sha1"
    expected: dict[str, str | None] = {}
    for edit in edits:
        original = start_blobs.get(edit.path)
        if original is not None and not isinstance(original, str):
            raise _replan_error(f"Provider returned an invalid blob ID for {edit.path!r}.")
        if edit.operation == "create":
            if original is not None:
                raise _replan_error(f"Plan expected to create existing file {edit.path!r}; re-plan.")
        elif original is None:
            raise _replan_error(f"Plan expected existing file {edit.path!r}; re-plan.")
        elif edit.original_sha is None or original.lower() != edit.original_sha.lower():
            raise _replan_error(f"Base blob for {edit.path!r} no longer matches the approved plan.")

        if edit.operation == "delete":
            expected[edit.path] = None
            continue
        assert edit.content is not None
        blob = git_blob_id(edit.content, object_format=object_format)
        if original is None or original.lower() != blob:
            expected[edit.path] = blob
    return expected


def _validated_tree_changes(comparison: object, start_revision: str, head_ref: str) -> dict[str, GitTreeChange]:
    if (
        not isinstance(comparison, GitTreeComparison)
        or comparison.base_revision.lower() != start_revision.lower()
        or not isinstance(comparison.changes, tuple)
    ):
        raise _replan_error(f"Provider returned an invalid tree comparison for ref {head_ref!r}.")
    changes: dict[str, GitTreeChange] = {}
    for change in comparison.changes:
        if not isinstance(change, GitTreeChange) or change.status not in ("added", "modified", "deleted"):
            raise _replan_error(f"Provider returned an invalid tree comparison for ref {head_ref!r}.")
        try:
            GitFileEdit(path=change.path, action="delete")
        except GitProviderError as exc:
            raise _replan_error(f"Provider returned an unsafe repository path: {change.path!r}.") from exc
        if change.path in changes:
            raise _replan_error(f"Provider returned duplicate paths for ref {head_ref!r}.")
        changes[change.path] = change
    return changes


def _validation_status(plan: Plan) -> tuple[Literal["success", "failure"], str]:
    """State and description of the ``greenkube/validation`` commit status (J5)."""
    file_plan = plan.file_plan
    render_valid = file_plan is not None and file_plan.validation.passed
    policy = plan.policy_result if isinstance(plan.policy_result, dict) else {}
    if "allowed" in policy:
        warnings = policy.get("warnings") if isinstance(policy.get("warnings"), list) else []
        policy_text = "policy passed" if policy.get("allowed") else "policy rejected"
        if policy.get("allowed") and warnings:
            policy_text += f" with {len(warnings)} warning(s)"
        policy_ok = bool(policy.get("allowed"))
    else:
        policy_text, policy_ok = "policy not recorded", True
    render_text = "render diff valid" if render_valid else "render validation failed"
    state: Literal["success", "failure"] = "success" if render_valid and policy_ok else "failure"
    return state, f"GreenKube: {render_text}; {policy_text}."


def _safe_error(error: Exception) -> str:
    return f"{type(error).__name__}: {str(error)[:200]}"


def _pull_request_head_sha(pull_request: PullRequestInfo) -> str | None:
    raw = pull_request.raw if isinstance(pull_request.raw, dict) else {}
    head = raw.get("head")
    # GitHub and Gitea nest the head commit; GitLab merge requests expose ``sha``.
    sha = head.get("sha") if isinstance(head, dict) else raw.get("sha")
    return sha if isinstance(sha, str) and GIT_OBJECT_SHA.fullmatch(sha) else None


def _pull_request_status(status: PullRequestStatus | str) -> str:
    return status.value if isinstance(status, PullRequestStatus) else str(status).lower()


def _plan_operation_type(plan: Plan | None) -> str:
    if plan is None:
        return "unknown"
    return "rollback" if plan.rollback_of_plan_id is not None else "apply"


def _replan_error(message: str) -> PlanWorkerError:
    return PlanWorkerError(
        message,
        plan_status=PlanStatus.STALE,
        action_status=ActionStatus.NONE,
    )


def _is_retryable(error: Exception) -> bool:
    if isinstance(error, PlanWorkerError):
        return error.retryable
    if isinstance(error, GitProviderError):
        return isinstance(error, GitProviderTransientError)
    return True


def _error_message(error: Exception) -> str:
    message = str(error).strip() or type(error).__name__
    return message[:1000]
