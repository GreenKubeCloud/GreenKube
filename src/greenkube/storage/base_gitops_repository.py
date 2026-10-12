"""Typed persistence contracts for GitOps discovery and automation state."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from greenkube.gitops.contracts import (
    ActionStatus,
    BindingStatus,
    Plan,
    PlanStatus,
    PreApplyWorkloadBaseline,
    PullRequestEvent,
    WorkloadSource,
    configured_binding_ttl,
)

MAX_PULL_REQUEST_POLLING_LEASE_SECONDS = 86_400
MAX_WORKLOAD_BINDING_PAGE_SIZE = 500
MAX_WORKLOAD_BINDING_CANDIDATES = 500


def _binding_identity_cursor(identity: tuple[str, str, str, str]) -> str:
    payload = json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _binding_identity_from_cursor(cursor: str) -> tuple[str, str, str, str]:
    if not cursor or len(cursor) > 2048:
        raise ValueError("invalid workload binding cursor")
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(cursor + padding))
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError) as error:
        raise ValueError("invalid workload binding cursor") from error
    if (
        not isinstance(decoded, list)
        or len(decoded) != 4
        or any(not isinstance(value, str) or not value or len(value) > 512 for value in decoded)
    ):
        raise ValueError("invalid workload binding cursor")
    return decoded[0], decoded[1], decoded[2], decoded[3]


def _binding_status_at(
    status: BindingStatus,
    *,
    observed_at: datetime,
    expires_at: datetime | None,
    now: datetime,
) -> tuple[BindingStatus, str | None]:
    if status not in {BindingStatus.RESOLVED, BindingStatus.AMBIGUOUS}:
        return status, None
    current = _as_utc(now)
    if expires_at is not None and _as_utc(expires_at) <= current:
        return BindingStatus.STALE, "source-expired"
    if current - _as_utc(observed_at) >= configured_binding_ttl():
        return BindingStatus.STALE, "source-observation-expired"
    return status, None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class GitOpsProviderConfig:
    """Provider configuration containing only a Kubernetes Secret reference."""

    id: str
    provider_type: str
    api_base_url: str | None = None
    secret_namespace: str | None = None
    secret_name: str | None = None
    secret_key: str | None = None
    enabled: bool = True
    capabilities: dict[str, Any] = field(default_factory=dict)
    status: str = "unverified"
    last_checked_at: datetime | None = None
    last_error_code: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.provider_type.strip():
            raise ValueError("provider id and type must not be blank")
        if (self.secret_name is None) != (self.secret_key is None):
            raise ValueError("secret_name and secret_key must be set together")
        if self.secret_namespace is not None and self.secret_name is None:
            raise ValueError("secret_namespace requires a Secret reference")
        if any(
            value is not None and not value.strip()
            for value in (self.secret_namespace, self.secret_name, self.secret_key)
        ):
            raise ValueError("Secret reference fields must not be blank")


@dataclass(frozen=True, slots=True)
class WorkloadBinding:
    """A workload's selected source and candidate sources."""

    cluster: str
    namespace: str
    workload_kind: str
    workload_name: str
    status: BindingStatus
    confidence: float
    source: WorkloadSource | None = None
    candidates: tuple[WorkloadSource, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    generation: int = 0
    candidates_truncated: bool = False

    def __post_init__(self) -> None:
        for value in (self.namespace, self.workload_kind, self.workload_name):
            if not value.strip():
                raise ValueError("workload binding identity fields must not be blank")
        status = self.status if isinstance(self.status, BindingStatus) else BindingStatus(self.status)
        object.__setattr__(self, "status", status)
        if not 0 <= self.confidence <= 1:
            raise ValueError("binding confidence must be between 0 and 1")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 0:
            raise ValueError("binding generation must be a non-negative integer")
        if not isinstance(self.candidates_truncated, bool):
            raise ValueError("candidates_truncated must be a boolean")
        candidates = tuple(self.candidates)
        candidate_ids = [candidate.id for candidate in candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("workload binding candidate source ids must be unique")
        if self.source is not None and all(candidate.id != self.source.id for candidate in candidates):
            candidates = (*candidates, self.source)
        for candidate in candidates:
            if (
                candidate.cluster != self.cluster
                or candidate.namespace != self.namespace
                or candidate.workload_kind != self.workload_kind
                or candidate.workload_name != self.workload_name
            ):
                raise ValueError("binding candidates must identify the same workload")
        object.__setattr__(self, "candidates", candidates)


@dataclass(frozen=True, slots=True)
class WorkloadBindingPage:
    """One bounded, stable cursor page of workload binding projections."""

    items: tuple[WorkloadBinding, ...]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class WorkloadBindingCoverage:
    """Freshness-aware workload binding counts and last observation time."""

    total: int = 0
    resolved: int = 0
    ambiguous: int = 0
    unresolved: int = 0
    blocked: int = 0
    stale: int = 0
    last_refresh: datetime | None = None

    @classmethod
    def from_bindings(cls, bindings: Iterable[WorkloadBinding]) -> WorkloadBindingCoverage:
        counts = {status: 0 for status in BindingStatus}
        latest: datetime | None = None
        total = 0
        for binding in bindings:
            total += 1
            counts[binding.status] += 1
            observed_at = _as_utc(binding.observed_at)
            latest = observed_at if latest is None else max(latest, observed_at)
        return cls(
            total=total,
            resolved=counts[BindingStatus.RESOLVED],
            ambiguous=counts[BindingStatus.AMBIGUOUS],
            unresolved=counts[BindingStatus.UNRESOLVED],
            blocked=counts[BindingStatus.BLOCKED],
            stale=counts[BindingStatus.STALE],
            last_refresh=latest,
        )

    @property
    def ratio(self) -> float:
        return self.resolved / self.total if self.total else 0.0


def freshen_workload_source(source: WorkloadSource, *, now: datetime) -> WorkloadSource:
    """Project expiry/observation freshness without changing persisted discovery state."""
    status, reason = _binding_status_at(
        source.status,
        observed_at=source.observed_at,
        expires_at=source.expires_at,
        now=now,
    )
    if status is source.status:
        return source
    return source.model_copy(update={"status": status, "reason": reason or source.reason})


def freshen_workload_binding(binding: WorkloadBinding, *, now: datetime) -> WorkloadBinding:
    """Project binding and candidate freshness consistently at one read timestamp."""
    candidates = tuple(freshen_workload_source(candidate, now=now) for candidate in binding.candidates)
    source = next((candidate for candidate in candidates if binding.source and candidate.id == binding.source.id), None)
    if source is None and binding.source is not None:
        source = freshen_workload_source(binding.source, now=now)
    status, reason = _binding_status_at(
        binding.status,
        observed_at=binding.observed_at,
        expires_at=binding.expires_at,
        now=now,
    )
    if status in {BindingStatus.RESOLVED, BindingStatus.AMBIGUOUS} and source is not None:
        source_status, source_reason = _binding_status_at(
            source.status,
            observed_at=source.observed_at,
            expires_at=source.expires_at,
            now=now,
        )
        if source_status is BindingStatus.STALE:
            status, reason = BindingStatus.STALE, source_reason
    return replace(
        binding,
        status=status,
        source=source,
        candidates=candidates,
        reason=reason or binding.reason,
    )


class WorkloadBindingEventType(StrEnum):
    """Durable lifecycle events for operator-approved binding overrides."""

    MANUAL_RESOLUTION_CREATED = "manual_resolution_created"
    MANUAL_RESOLUTION_EXPIRED = "manual_resolution_expired"
    MANUAL_RESOLUTION_INVALIDATED = "manual_resolution_invalidated"


@dataclass(frozen=True, slots=True)
class WorkloadBindingEvent:
    """Credential-free audit record for a manual binding lifecycle transition."""

    cluster: str
    namespace: str
    workload_kind: str
    workload_name: str
    event_type: WorkloadBindingEventType | str
    actor: str
    source_id: str | None = None
    source_generation: str | None = None
    expires_at: datetime | None = None
    details: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    id: int | None = None
    event_key: str = field(init=False)

    def __post_init__(self) -> None:
        try:
            event_type = WorkloadBindingEventType(self.event_type)
        except ValueError as error:
            raise ValueError("unsupported workload binding audit event type") from error
        object.__setattr__(self, "event_type", event_type)
        if any(not value.strip() for value in (self.cluster, self.namespace, self.workload_kind, self.workload_name)):
            raise ValueError("workload binding audit identity must not be blank")
        if not self.actor.strip() or len(self.actor) > 80:
            raise ValueError("workload binding audit actor must contain 1 to 80 characters")
        if not isinstance(self.details, dict):
            raise ValueError("workload binding audit details must be an object")
        identity = "\0".join(
            (
                self.cluster,
                self.namespace,
                self.workload_kind,
                self.workload_name,
                event_type.value,
                self.source_id or "",
                self.source_generation or "",
            )
        )
        object.__setattr__(self, "event_key", hashlib.sha256(identity.encode("utf-8")).hexdigest())


class WorkloadBindingConflictError(RuntimeError):
    """Raised when a manual resolution's persisted generation is no longer current."""


@dataclass(frozen=True, slots=True)
class AutomationPlanRecord:
    """Plan contract plus persisted policy and update metadata."""

    plan: Plan
    policy_result: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class WebhookDelivery:
    """A deduplicated webhook delivery and its typed pull-request event."""

    id: int
    event_type: str
    event: PullRequestEvent | None
    status: str
    received_at: datetime
    processed_at: datetime | None = None
    error_code: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    provider_id: str | None = None
    delivery_id: str | None = None


@dataclass(frozen=True, slots=True)
class WorkerHeartbeat:
    """Current heartbeat and lease state for an automation worker."""

    worker_id: str
    status: str = "starting"
    heartbeat_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    lease_id: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    lease_operation_id: int | None = None
    lease_plan_operation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.worker_id.strip() or not self.status.strip():
            raise ValueError("worker id and status must not be blank")


@dataclass(frozen=True, slots=True)
class AutomationBacklogSummary:
    """Low-cardinality aggregate counts used by automation health metrics."""

    queued_operations: int = 0
    running_operations: int = 0
    retrying_operations: int = 0
    failed_operations: int = 0
    stuck_operations: int = 0
    applying_plans: int = 0
    merged_plans: int = 0
    stuck_plans: int = 0
    sync_failed_plans: int = 0
    pending_webhooks: int = 0
    stuck_webhooks: int = 0
    worker_count: int = 0
    stale_workers: int = 0
    latest_worker_heartbeat_timestamp: float = 0.0

    def as_metrics(self) -> dict[str, int | float]:
        return {
            "queued_operations": self.queued_operations,
            "running_operations": self.running_operations,
            "retrying_operations": self.retrying_operations,
            "failed_operations": self.failed_operations,
            "stuck_operations": self.stuck_operations,
            "applying_plans": self.applying_plans,
            "merged_plans": self.merged_plans,
            "stuck_plans": self.stuck_plans,
            "sync_failed_plans": self.sync_failed_plans,
            "pending_webhooks": self.pending_webhooks,
            "stuck_webhooks": self.stuck_webhooks,
            "worker_count": self.worker_count,
            "stale_workers": self.stale_workers,
            "latest_worker_heartbeat_timestamp": self.latest_worker_heartbeat_timestamp,
        }


@dataclass(frozen=True, slots=True)
class PullRequestPollingLease:
    """Fenced ownership of the plan worker's singleton PR-polling fallback."""

    owner_id: str
    fencing_token: int
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.owner_id.strip():
            raise ValueError("polling lease owner must not be blank")
        if type(self.fencing_token) is not int or self.fencing_token <= 0:
            raise ValueError("polling lease fencing token must be a positive integer")


class PullRequestPollingLeaseNotOwnedError(RuntimeError):
    """Raised when a polling worker tries to renew a lease it no longer owns."""


class GitOpsProviderRepository(ABC):
    """Persistence port for Git provider configuration and allowlists."""

    @abstractmethod
    async def save_provider_config(self, provider: GitOpsProviderConfig) -> GitOpsProviderConfig: ...

    @abstractmethod
    async def get_provider_config(self, provider_id: str) -> GitOpsProviderConfig | None: ...

    @abstractmethod
    async def list_provider_configs(self, *, enabled_only: bool = False) -> list[GitOpsProviderConfig]: ...

    @abstractmethod
    async def add_provider_allowlist(self, provider_id: str, repository_pattern: str) -> None: ...

    @abstractmethod
    async def list_provider_allowlists(self, provider_id: str) -> list[str]: ...

    @abstractmethod
    async def remove_provider_allowlist(self, provider_id: str, repository_pattern: str) -> bool: ...


class WorkloadBindingRepository(ABC):
    """Persistence port for workload sources, bindings and binding candidates."""

    async def save_discovered_workload_binding(
        self,
        binding: WorkloadBinding,
        *,
        expected_generation: int | None,
        events: Sequence[WorkloadBindingEvent] = (),
    ) -> WorkloadBinding | None:
        """Persist discovery output only if the previously observed generation is unchanged.

        Durable adapters should override this method with a transactional CAS. The
        fallback keeps simple in-memory adapters compatible with the port.
        """
        current = await self.get_workload_binding(
            binding.cluster,
            binding.namespace,
            binding.workload_kind,
            binding.workload_name,
        )
        if (current.generation if current is not None else None) != expected_generation:
            return None
        if not events:
            return await self.save_workload_binding(binding)
        saved = await self.save_workload_binding_with_event(binding, events[0])
        for event in events[1:]:
            await self.record_workload_binding_event(event)
        return saved

    @abstractmethod
    async def save_workload_binding(self, binding: WorkloadBinding) -> WorkloadBinding: ...

    @abstractmethod
    async def save_workload_binding_with_event(
        self,
        binding: WorkloadBinding,
        event: WorkloadBindingEvent,
    ) -> WorkloadBinding: ...

    @abstractmethod
    async def save_manual_workload_binding(
        self,
        binding: WorkloadBinding,
        event: WorkloadBindingEvent,
        *,
        expected_candidate_id: str,
        expected_generation_token: str,
        now: datetime,
    ) -> WorkloadBinding: ...

    @abstractmethod
    async def get_workload_binding(
        self, cluster: str, namespace: str, workload_kind: str, workload_name: str
    ) -> WorkloadBinding | None: ...

    @abstractmethod
    async def list_workload_bindings(self, *, cluster: str | None = None) -> list[WorkloadBinding]: ...

    async def list_workload_bindings_page(
        self,
        *,
        cluster: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        statuses: Sequence[BindingStatus] | None = None,
        candidate_limit: int = 50,
        now: datetime | None = None,
    ) -> WorkloadBindingPage:
        """Return one bounded page; SQL adapters should push filtering and limits into storage."""
        _validate_binding_page(limit=limit, candidate_limit=candidate_limit)
        selected_statuses = None if statuses is None else {BindingStatus(status) for status in statuses}
        if selected_statuses is not None and not selected_statuses:
            return WorkloadBindingPage((), None, False)
        current_at = _as_utc(now or datetime.now(timezone.utc))
        bindings = [
            freshen_workload_binding(binding, now=current_at)
            for binding in await self.list_workload_bindings(cluster=cluster)
        ]
        if selected_statuses is not None:
            bindings = [binding for binding in bindings if binding.status in selected_statuses]
        bindings.sort(key=_workload_binding_identity)
        if cursor is not None:
            cursor_identity = _binding_identity_from_cursor(cursor)
            if cluster is not None and cursor_identity[0] != cluster:
                raise ValueError("workload binding cursor does not match the selected cluster")
            bindings = [binding for binding in bindings if _workload_binding_identity(binding) > cursor_identity]
        selected = bindings[: limit + 1]
        has_more = len(selected) > limit
        page_items: list[WorkloadBinding] = []
        for binding in selected[:limit]:
            ordered_candidates = sorted(
                binding.candidates,
                key=lambda source: (
                    0 if binding.source is not None and source.id == binding.source.id else 1,
                    -source.confidence,
                    source.id,
                ),
            )
            page_items.append(
                replace(
                    binding,
                    candidates=tuple(ordered_candidates[:candidate_limit]),
                    source=(
                        next(
                            (
                                candidate
                                for candidate in ordered_candidates[:candidate_limit]
                                if binding.source is not None and candidate.id == binding.source.id
                            ),
                            binding.source,
                        )
                    ),
                    candidates_truncated=len(ordered_candidates) > candidate_limit or binding.candidates_truncated,
                )
            )
        next_cursor = _binding_identity_cursor(_workload_binding_identity(page_items[-1])) if has_more else None
        return WorkloadBindingPage(tuple(page_items), next_cursor, has_more)

    async def get_workload_binding_coverage(
        self,
        *,
        cluster: str | None = None,
        now: datetime | None = None,
    ) -> WorkloadBindingCoverage:
        """Return freshness-aware status counts; SQL adapters should aggregate in storage."""
        current_at = _as_utc(now or datetime.now(timezone.utc))
        bindings = [
            freshen_workload_binding(binding, now=current_at)
            for binding in await self.list_workload_bindings(cluster=cluster)
        ]
        return WorkloadBindingCoverage.from_bindings(bindings)

    @abstractmethod
    async def record_workload_binding_event(self, event: WorkloadBindingEvent) -> WorkloadBindingEvent: ...

    @abstractmethod
    async def list_workload_binding_events(
        self,
        *,
        cluster: str,
        namespace: str,
        workload_kind: str,
        workload_name: str,
        limit: int = 100,
    ) -> list[WorkloadBindingEvent]: ...


def _validate_binding_page(*, limit: int, candidate_limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_WORKLOAD_BINDING_PAGE_SIZE:
        raise ValueError(f"workload binding page limit must be between 1 and {MAX_WORKLOAD_BINDING_PAGE_SIZE}")
    if (
        isinstance(candidate_limit, bool)
        or not isinstance(candidate_limit, int)
        or not 1 <= candidate_limit <= MAX_WORKLOAD_BINDING_CANDIDATES
    ):
        raise ValueError(f"workload binding candidate limit must be between 1 and {MAX_WORKLOAD_BINDING_CANDIDATES}")


def _workload_binding_identity(binding: WorkloadBinding) -> tuple[str, str, str, str]:
    return binding.cluster, binding.namespace, binding.workload_kind, binding.workload_name


class AutomationPlanRepository(ABC):
    """Persistence port for automation plans, recommendation joins and action status."""

    async def prune_automation_history(self, *, cutoff: datetime) -> dict[str, int]:
        """Prune retained payloads when supported by a durable backend."""
        raise NotImplementedError("automation history retention is not supported by this repository")

    @abstractmethod
    async def save_plan(
        self,
        plan: Plan,
        *,
        policy_result: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> Plan: ...

    async def save_plan_if_absent(
        self,
        plan: Plan,
        *,
        policy_result: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> Plan:
        """Persist a plan only when its deterministic ID is unused.

        SQL repositories override this with an atomic insert. This fallback
        preserves compatibility for simple in-memory adapters.
        """
        existing = await self.get_plan(plan.plan_id)
        if existing is not None:
            return existing
        return await self.save_plan(plan, policy_result=policy_result, actor=actor)

    async def set_plan_pre_apply_baseline_if_absent(
        self,
        plan_id: str,
        baseline: PreApplyWorkloadBaseline,
    ) -> Plan | None:
        """Set the pre-apply baseline once; SQL repositories provide an atomic implementation."""
        existing = await self.get_plan(plan_id)
        if existing is None or existing.pre_apply_baseline is not None:
            return existing
        return await self.save_plan(existing.model_copy(update={"pre_apply_baseline": baseline}))

    @abstractmethod
    async def get_plan(self, plan_id: str) -> Plan | None: ...

    @abstractmethod
    async def get_plan_record(self, plan_id: str) -> AutomationPlanRecord | None: ...

    async def set_plan_policy_result(
        self,
        plan_id: str,
        policy_result: dict[str, Any],
        *,
        expected_digest: str,
        expected_statuses: Sequence[PlanStatus] | None = None,
        actor: str = "system",
    ) -> Plan | None:
        """Persist the latest policy evaluation without changing plan identity."""
        plan = await self.get_plan(plan_id)
        if plan is None or plan.digest != expected_digest:
            return None
        if expected_statuses is not None and plan.status not in expected_statuses:
            return None
        updated = plan.model_copy(update={"policy_result": dict(policy_result)})
        return await self.save_plan(updated, policy_result=policy_result, actor=actor)

    @abstractmethod
    async def list_plans(self, *, status: PlanStatus | None = None, source_id: str | None = None) -> list[Plan]: ...

    async def list_plans_for_workload(
        self,
        *,
        cluster: str,
        namespace: str,
        workload_kind: str,
        workload_name: str,
        statuses: Sequence[PlanStatus] | None = None,
    ) -> list[Plan]:
        plans = await self.list_plans()
        return [
            plan
            for plan in plans
            if plan.source.cluster == cluster
            and plan.source.namespace == namespace
            and plan.source.workload_kind == workload_kind
            and plan.source.workload_name == workload_name
            and (statuses is None or plan.status in statuses)
        ]

    @abstractmethod
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

    @abstractmethod
    async def list_recommendation_ids_for_plan(self, plan_id: str) -> list[int]: ...

    @abstractmethod
    async def list_plans_for_recommendation(self, recommendation_id: int) -> list[Plan]: ...

    @abstractmethod
    async def set_recommendation_action_status(
        self,
        recommendation_ids: Sequence[int],
        status: ActionStatus,
        *,
        expected_statuses: Sequence[ActionStatus] | None = None,
        actor: str = "system",
        plan_id: str | None = None,
    ) -> int: ...

    @abstractmethod
    async def get_recommendation_action_status(self, recommendation_id: int) -> ActionStatus | None: ...

    @abstractmethod
    async def list_recommendations_by_action_status(self, status: ActionStatus) -> list[int]: ...


class WebhookDeliveryRepository(ABC):
    """Persistence port for deduplicated provider webhook deliveries."""

    @abstractmethod
    async def record_webhook_delivery(
        self, event: PullRequestEvent, *, event_type: str = "pull_request"
    ) -> tuple[WebhookDelivery, bool]: ...

    @abstractmethod
    async def get_webhook_delivery(self, provider_id: str, delivery_id: str) -> WebhookDelivery | None: ...

    @abstractmethod
    async def update_webhook_delivery(
        self,
        provider_id: str,
        delivery_id: str,
        *,
        status: str,
        processed_at: datetime | None = None,
        error_code: str | None = None,
        expected_statuses: Sequence[str] | None = None,
    ) -> WebhookDelivery | None: ...

    @abstractmethod
    async def claim_webhook_delivery(
        self,
        provider_id: str,
        delivery_id: str,
        *,
        stale_before: datetime,
    ) -> bool: ...

    @abstractmethod
    async def list_pending_webhook_deliveries(
        self,
        *,
        limit: int,
        stale_before: datetime,
    ) -> list[WebhookDelivery]: ...


class AutomationWorkerRepository(ABC):
    """Persistence port for worker heartbeats and leases."""

    @abstractmethod
    async def upsert_worker_heartbeat(self, heartbeat: WorkerHeartbeat) -> WorkerHeartbeat: ...

    @abstractmethod
    async def get_worker_heartbeat(self, worker_id: str) -> WorkerHeartbeat | None: ...

    @abstractmethod
    async def list_worker_heartbeats(self, *, status: str | None = None) -> list[WorkerHeartbeat]: ...

    @abstractmethod
    async def prune_worker_heartbeats(self, *, cutoff: datetime) -> int: ...


class PullRequestPollingLeaseRepository(ABC):
    """Persistence port for singleton ownership of PR polling across replicas."""

    @abstractmethod
    async def try_acquire_pr_polling_lease(
        self,
        owner_id: str,
        *,
        lease_seconds: int,
    ) -> PullRequestPollingLease | None:
        """Acquire an absent or expired lease and atomically advance its fencing token."""

    @abstractmethod
    async def renew_pr_polling_lease(
        self,
        lease: PullRequestPollingLease,
        *,
        lease_seconds: int,
    ) -> PullRequestPollingLease:
        """Extend a live lease only when owner and fencing token still match."""

    @abstractmethod
    async def release_pr_polling_lease(self, lease: PullRequestPollingLease) -> bool:
        """Expire the matching live lease; stale owners cannot release successors."""


class GitOpsRepository(
    GitOpsProviderRepository,
    WorkloadBindingRepository,
    AutomationPlanRepository,
    WebhookDeliveryRepository,
    AutomationWorkerRepository,
    PullRequestPollingLeaseRepository,
):
    """Aggregate persistence port implemented by the SQLite and PostgreSQL adapters."""

    async def get_automation_backlog_summary(self, *, stale_before: datetime) -> AutomationBacklogSummary:
        """Return bounded automation queue and worker-health aggregates."""
        raise NotImplementedError("automation backlog metrics are not supported by this repository")
