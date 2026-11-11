"""The automation controller loop and the controller's plan executor (roadmap M8, F8).

Before M8 the automation housekeeping was spread over three places: the plan expiry
and retention sweep ran in *every API replica*, while the webhook outbox, PR
polling, branch cleanup and pending-PR recovery ran next to the plan queue in the
worker (inline in the API, or in a dedicated Deployment). This module gathers them
into one leader-gated controller loop, :class:`AutomationController`, whose every
tick runs under one database lease (the singleton ``gitops_pr_polling_lease`` row)
so that at most one process does this work even without Kubernetes leader
election:

==================  ==================================================  =============================
Step                What it does                                        Cadence
==================  ==================================================  =============================
``operations``      Reconcile plans whose operation exhausted retries   every 60 s
``expiry``          Mark plans past their TTL stale                     every 60 s
``webhooks``        Reconcile the durable webhook outbox                every tick (delivery only)
``pr_polling``      Poll one page of open PRs, then branch cleanup      ``GITOPS_PR_POLL_INTERVAL``
``recovery``        Quarantine stale pending PR attempts                once per process
``retention``       Prune plan history (90 d) and worker heartbeats     daily
==================  ==================================================  =============================

The business logic of each step is unchanged and lives where it did (plan service,
lifecycle reconciler, polling and branch-cleanup runtimes); this module only
schedules it. Automation is always on (F12): the PR-tracking steps (webhook outbox,
PR polling, recovery) run while :class:`~greenkube.automation.readiness.AutomationReadiness`
reports tracking active (a provider has credentials, or webhooks are configured), so
creating or deleting the credentials Secret switches them without a restart; expiry
and retention always run.

:class:`PlanExecutor` claims and executes queued plan operations. It runs in the
controller (leader-gated); the optional dedicated worker runs the same executor
for extra capacity. Operation leases make concurrent executors safe.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, cast

from greenkube.automation.branch_cleanup_runtime import BranchCleanupRuntime
from greenkube.automation.pr_lifecycle_runtime import create_pull_request_lifecycle_reconciler
from greenkube.automation.pr_polling_runtime import (
    PollablePullRequestRepository,
    PullRequestPollingRuntime,
)
from greenkube.core.leader_election import current_leader_fence
from greenkube.core.loops import error_code, report_loop_failure
from greenkube.core.observability import (
    record_automation_failure,
    record_automation_worker_heartbeat,
    set_automation_worker_up,
)
from greenkube.gitops.contracts import Plan
from greenkube.storage.base_gitops_repository import (
    PullRequestPollingLease,
    PullRequestPollingLeaseRepository,
    WebhookDelivery,
)
from greenkube.storage.base_pull_request_repository import MAX_PULL_REQUEST_POLL_BATCH_SIZE

logger = logging.getLogger(__name__)

AUTOMATION_CONTROLLER_INTERVAL_SECONDS = 5.0
AUTOMATION_CONTROLLER_LEASE_SECONDS = 90
PLAN_EXPIRY_INTERVAL_SECONDS = 60.0
RETENTION_INTERVAL_SECONDS = 86_400.0
PLAN_HISTORY_RETENTION_DAYS = 90
WORKER_HEARTBEAT_RETENTION_DAYS = 7
PENDING_PULL_REQUEST_RECOVERY_GRACE_SECONDS = 900
WEBHOOK_OUTBOX_BATCH_SIZE = 10
WEBHOOK_OUTBOX_MAX_BATCHES_PER_TICK = 5
WEBHOOK_PROCESSING_LEASE_SECONDS = 300
PLAN_EXECUTOR_INTERVAL_SECONDS = 5.0
PLAN_EXECUTOR_MAX_OPERATIONS_PER_TICK = 10

STEP_OPERATIONS = "operations"
STEP_EXPIRY = "expiry"
STEP_WEBHOOKS = "webhooks"
STEP_PR_POLLING = "pr_polling"
STEP_RECOVERY = "recovery"
STEP_RETENTION = "retention"
_FAILURE_REASONS = {STEP_WEBHOOKS: "webhook", STEP_PR_POLLING: "polling", STEP_RECOVERY: "worker"}


class PlanMaintenanceService(Protocol):
    """Plan-service methods the automation controller schedules."""

    async def expire_due_plans(self) -> Sequence[Plan]: ...

    async def prune_retained_history(self, *, cutoff: datetime) -> dict[str, int]: ...


class _ExhaustedOperationReconciler(Protocol):
    async def reconcile_exhausted_operations(self) -> Sequence[Plan]: ...


class _WorkerHeartbeatRepository(Protocol):
    async def prune_worker_heartbeats(self, *, cutoff: datetime) -> int: ...


class _PendingPullRequestRecoveryRepository(Protocol):
    async def reconcile_open_pull_requests(self, *, stale_before: datetime, limit: int) -> int: ...


class _WebhookOutboxRepository(Protocol):
    async def list_pending_webhook_deliveries(
        self,
        *,
        limit: int,
        stale_before: datetime,
    ) -> list[WebhookDelivery]: ...

    async def claim_webhook_delivery(
        self,
        provider_id: str,
        delivery_id: str,
        *,
        stale_before: datetime,
    ) -> bool: ...

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


@dataclass
class AutomationControllerSummary:
    """Secret-free outcome of one automation controller tick."""

    lease_acquired: bool = False
    steps: dict[str, str] = field(default_factory=dict)

    @property
    def failed_steps(self) -> list[str]:
        return [step for step, outcome in self.steps.items() if outcome == "failed"]


class AutomationController:
    """One leader-gated tick of automation housekeeping under the singleton DB lease."""

    def __init__(
        self,
        *,
        plan_service: PlanMaintenanceService,
        lease_repository: PullRequestPollingLeaseRepository,
        plan_worker: Any | None = None,
        pull_request_polling: PullRequestPollingRuntime | None = None,
        delivery_enabled: bool | Callable[[], bool] = False,
        owner_id: str | None = None,
        lease_seconds: int = AUTOMATION_CONTROLLER_LEASE_SECONDS,
        expiry_interval_seconds: float = PLAN_EXPIRY_INTERVAL_SECONDS,
        retention_interval_seconds: float = RETENTION_INTERVAL_SECONDS,
        pr_poll_interval_seconds: float | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.plan_service = plan_service
        self.lease_repository = lease_repository
        self.plan_worker = plan_worker
        self.pull_request_polling = pull_request_polling
        self._delivery_enabled = delivery_enabled
        self.owner_id = owner_id or f"automation-controller:{uuid.uuid4()}"
        self.lease_seconds = lease_seconds
        self.intervals = {
            STEP_OPERATIONS: float(expiry_interval_seconds),
            STEP_EXPIRY: float(expiry_interval_seconds),
            STEP_WEBHOOKS: 0.0,
            STEP_PR_POLLING: float(
                pr_poll_interval_seconds
                if pr_poll_interval_seconds is not None
                else (pull_request_polling.interval_seconds if pull_request_polling is not None else 0.0)
            ),
            STEP_RETENTION: float(retention_interval_seconds),
        }
        self._clock = clock
        self._monotonic = monotonic
        self._last_run: dict[str, float] = {}
        self._recovered = False

    async def tick(self) -> AutomationControllerSummary:
        summary = AutomationControllerSummary()
        steps = self._steps()
        if not any(self._due(step) for step, _ in steps):
            # Nothing is due (e.g. delivery off between two expiry passes): do not
            # write the lease row every 5 s for nothing.
            summary.steps = {step: "not_due" for step, _ in steps}
            return summary
        lease = await self.lease_repository.try_acquire_pr_polling_lease(
            self.owner_id,
            lease_seconds=self.lease_seconds,
        )
        if lease is None:
            logger.debug("Automation controller tick skipped: another process holds the automation lease.")
            return summary
        summary.lease_acquired = True
        try:
            for step, run in steps:
                if not self._due(step):
                    summary.steps[step] = "not_due"
                    continue
                current_leader_fence().check(f"automation controller step {step}")
                lease = await self.lease_repository.renew_pr_polling_lease(lease, lease_seconds=self.lease_seconds)
                try:
                    succeeded = await run(lease)
                except Exception as exc:
                    succeeded = False
                    logger.warning(
                        "Automation controller step %s failed; it will be retried. error_code=%s error_type=%s",
                        step,
                        error_code(exc),
                        type(exc).__name__,
                    )
                    record_automation_failure(_FAILURE_REASONS.get(step, "other"))
                summary.steps[step] = "ok" if succeeded else "failed"
                if succeeded or step != STEP_RETENTION:
                    # A failed retention pass is retried next tick, as the old API sweep did.
                    self._last_run[step] = self._monotonic()
        finally:
            released = await self.lease_repository.release_pr_polling_lease(lease)
            if not released:
                logger.warning("The automation controller lease was no longer owned at the end of the tick.")
        failed = summary.failed_steps
        if failed:
            report_loop_failure(f"{failed[0]}_failed")
        return summary

    @property
    def delivery_enabled(self) -> bool:
        """Whether the PR-tracking steps run now (re-evaluated every tick, F12)."""
        flag = self._delivery_enabled
        return bool(flag() if callable(flag) else flag)

    def _steps(self):
        steps: list[tuple[str, Callable[[PullRequestPollingLease], Any]]] = [
            (STEP_OPERATIONS, self._reconcile_exhausted_operations),
            (STEP_EXPIRY, self._expire_due_plans),
        ]
        if self.delivery_enabled and self.plan_worker is not None:
            steps.append((STEP_WEBHOOKS, self._process_webhooks))
            if self.pull_request_polling is not None:
                steps.append((STEP_PR_POLLING, self._poll_pull_requests))
            if not self._recovered:
                steps.append((STEP_RECOVERY, self._recover_pending_pull_requests))
        steps.append((STEP_RETENTION, self._retention))
        return steps

    def _due(self, step: str) -> bool:
        if step == STEP_RECOVERY:
            return not self._recovered
        last = self._last_run.get(step)
        return last is None or self._monotonic() - last >= self.intervals.get(step, 0.0)

    async def _reconcile_exhausted_operations(self, _lease: PullRequestPollingLease) -> bool:
        reconcile = getattr(self.plan_service, "reconcile_exhausted_operations", None)
        if not callable(reconcile):
            return True
        reconciled = await cast(_ExhaustedOperationReconciler, self.plan_service).reconcile_exhausted_operations()
        if reconciled:
            logger.warning("Reconciled %d applying plans after exhausted operations.", len(reconciled))
        return True

    async def _expire_due_plans(self, _lease: PullRequestPollingLease) -> bool:
        expired = await self.plan_service.expire_due_plans()
        if expired:
            logger.info("Plan expiry sweep marked %d plans stale.", len(expired))
        return True

    async def _process_webhooks(self, _lease: PullRequestPollingLease) -> bool:
        for _ in range(WEBHOOK_OUTBOX_MAX_BATCHES_PER_TICK):
            if not await process_pending_webhook_deliveries(self.plan_worker):
                break
        return True

    async def _poll_pull_requests(self, lease: PullRequestPollingLease) -> bool:
        assert self.pull_request_polling is not None
        await self.pull_request_polling.poll_with_lease(lease)
        return True

    async def _recover_pending_pull_requests(self, _lease: PullRequestPollingLease) -> bool:
        self._recovered = True
        repository = getattr(self.plan_worker, "pull_request_repository", None)
        if repository is None:
            return True
        quarantined = await cast(_PendingPullRequestRecoveryRepository, repository).reconcile_open_pull_requests(
            stale_before=self._clock() - timedelta(seconds=PENDING_PULL_REQUEST_RECOVERY_GRACE_SECONDS),
            limit=MAX_PULL_REQUEST_POLL_BATCH_SIZE,
        )
        if quarantined:
            logger.warning(
                "Quarantined %d stale pending pull-request attempts without plan/provider/PR references.",
                quarantined,
            )
        return True

    async def _retention(self, _lease: PullRequestPollingLease) -> bool:
        now = self._clock()
        pruned = await self.plan_service.prune_retained_history(
            cutoff=now - timedelta(days=PLAN_HISTORY_RETENTION_DAYS)
        )
        if any(pruned.values()):
            logger.info("Automation history retention pruned %s.", pruned)
        heartbeats = getattr(self.plan_worker, "plans", None) if self.delivery_enabled else None
        if heartbeats is not None and callable(getattr(heartbeats, "prune_worker_heartbeats", None)):
            await cast(_WorkerHeartbeatRepository, heartbeats).prune_worker_heartbeats(
                cutoff=now - timedelta(days=WORKER_HEARTBEAT_RETENTION_DAYS)
            )
        return True


class PlanExecutor:
    """Claim and execute queued plan operations, a bounded batch per tick."""

    def __init__(
        self,
        plan_worker: Any,
        *,
        worker_id: str | None = None,
        max_operations_per_tick: int = PLAN_EXECUTOR_MAX_OPERATIONS_PER_TICK,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        active: Callable[[], bool] | None = None,
    ) -> None:
        if type(max_operations_per_tick) is not int or max_operations_per_tick < 1:
            raise ValueError("max_operations_per_tick must be a positive integer")
        self.plan_worker = plan_worker
        self.worker_id = worker_id or str(uuid.uuid4())
        self.max_operations_per_tick = max_operations_per_tick
        self._clock = clock
        self.active = active

    async def tick(self) -> int:
        if self.active is not None and not self.active():
            # Delivery is dormant (no deliverable repository, or pauseDelivery): queued
            # operations wait; creating the credentials Secret resumes them (F12).
            set_automation_worker_up(False)
            return 0
        set_automation_worker_up(True)
        record_automation_worker_heartbeat(self._clock().timestamp())
        processed = 0
        while processed < self.max_operations_per_tick:
            # A controller that lost the Lease must not claim more operations (A14 fence).
            current_leader_fence().check("plan operation claim")
            if not await self.plan_worker.process_once(self.worker_id):
                break
            processed += 1
        return processed

    def standby(self) -> None:
        """This controller no longer holds the Lease: it executes nothing."""
        set_automation_worker_up(False)

    def close(self) -> None:
        set_automation_worker_up(False)


def create_branch_cleanup_runtime(plan_worker: Any) -> BranchCleanupRuntime:
    return BranchCleanupRuntime(
        pull_request_repository=cast(Any, plan_worker.pull_request_repository),
        plan_repository=cast(Any, plan_worker.plans),
        operation_repository=cast(Any, plan_worker.operations),
        provider_resolver=plan_worker.provider_resolver,
    )


def create_pr_polling_runtime(plan_worker: Any) -> PullRequestPollingRuntime:
    return PullRequestPollingRuntime(
        cast(PollablePullRequestRepository, plan_worker.pull_request_repository),
        create_pull_request_lifecycle_reconciler(plan_worker),
        cast(PullRequestPollingLeaseRepository, plan_worker.plans),
        branch_cleanup=create_branch_cleanup_runtime(plan_worker),
    )


def create_automation_controller(
    plan_runtime: Any, *, delivery_enabled: bool | Callable[[], bool]
) -> AutomationController:
    """Compose the controller loop from a :class:`~greenkube.automation.plan_runtime.PlanRuntime`."""
    worker = plan_runtime.worker
    polling = create_pr_polling_runtime(worker) if delivery_enabled is not False else None
    return AutomationController(
        plan_service=plan_runtime.plan_service,
        lease_repository=cast(PullRequestPollingLeaseRepository, plan_runtime.gitops_repository),
        plan_worker=worker,
        pull_request_polling=polling,
        delivery_enabled=delivery_enabled,
    )


async def process_pending_webhook_deliveries(plan_worker: Any) -> bool:
    """Reconcile one batch of the durable webhook outbox; return whether a delivery was claimed."""
    raw_repository = getattr(plan_worker, "plans", None)
    if raw_repository is None:
        return False
    repository = cast(_WebhookOutboxRepository, raw_repository)

    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(seconds=WEBHOOK_PROCESSING_LEASE_SECONDS)
    deliveries = await repository.list_pending_webhook_deliveries(
        limit=WEBHOOK_OUTBOX_BATCH_SIZE,
        stale_before=stale_before,
    )
    if not deliveries:
        return False

    reconciler = create_pull_request_lifecycle_reconciler(plan_worker)
    worked = False
    for delivery in deliveries:
        event = delivery.event
        if event is None:
            provider_id = delivery.provider_id
            delivery_id = delivery.delivery_id
            if not isinstance(provider_id, str) or not isinstance(delivery_id, str):
                logger.error("Webhook outbox row %s has no valid provider or delivery ID.", delivery.id)
                record_automation_failure("webhook")
                continue
            if not await repository.claim_webhook_delivery(provider_id, delivery_id, stale_before=stale_before):
                continue
            worked = True
            await repository.update_webhook_delivery(
                provider_id,
                delivery_id,
                status="rejected",
                processed_at=now,
                error_code="invalid_persisted_event",
                expected_statuses=("processing",),
            )
            continue
        if not await repository.claim_webhook_delivery(
            event.provider_id,
            event.delivery_id,
            stale_before=stale_before,
        ):
            continue
        worked = True
        try:
            result = await reconciler.reconcile(event, newly_deduplicated=True)
        except Exception as error:
            await repository.update_webhook_delivery(
                event.provider_id,
                event.delivery_id,
                status="retryable",
                error_code="lifecycle_reconcile_failed",
                expected_statuses=("processing",),
            )
            logger.warning(
                "Webhook outbox reconciliation failed for delivery %s (%s).",
                event.delivery_id,
                type(error).__name__,
            )
            record_automation_failure("webhook")
            continue

        outcome = getattr(getattr(result, "outcome", None), "value", None)
        if outcome == "unlinked":
            # Unknown pull requests are terminal and pruned by retention (D11, BUG-53).
            status, delivery_error = "ignored", "pull_request_unlinked"
        elif outcome == "mismatched":
            status, delivery_error = "rejected", "pull_request_mismatched"
        elif isinstance(outcome, str):
            status, delivery_error = "processed", None
        else:
            status, delivery_error = "retryable", "invalid_lifecycle_outcome"
        await repository.update_webhook_delivery(
            event.provider_id,
            event.delivery_id,
            status=status,
            processed_at=now if status in {"processed", "rejected", "ignored"} else None,
            error_code=delivery_error,
            expected_statuses=("processing",),
        )
    return worked
