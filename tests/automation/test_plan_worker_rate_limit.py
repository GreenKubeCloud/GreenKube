"""A rate-limited provider reschedules the plan operation instead of failing it (provider resilience)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import cast

import pytest

from greenkube.automation.git.base import GitProviderRateLimitError
from greenkube.gitops.contracts import Operation, PlanStatus
from greenkube.storage.base_plan_operation_repository import PlanOperationLease
from tests.automation.test_plan_worker import (  # pyrefly: ignore[missing-import]
    FakeOperationRepository,
    FakeProviderResolver,
    _worker,
)


class RetryAtOperationRepository(FakeOperationRepository):
    def __init__(self, *leases: PlanOperationLease) -> None:
        super().__init__(*leases)
        self.retry_ats: list[datetime | None] = []

    async def mark_failed(
        self,
        lease: PlanOperationLease,
        error: str,
        *,
        retryable: bool = True,
        retry_at: datetime | None = None,
    ) -> Operation:
        self.retry_ats.append(retry_at)
        return await super().mark_failed(lease, error, retryable=retryable)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [403, 429])
async def test_rate_limit_reschedules_at_retry_at_and_never_marks_permission_denied(status_code):
    retry_at = datetime.now(timezone.utc) + timedelta(minutes=12)
    worker, operations, plans, _, provider = _worker(
        provider_error=GitProviderRateLimitError("github rate limit", status_code=status_code, retry_at=retry_at)
    )
    queue = RetryAtOperationRepository(*operations.leases)
    worker.operations = queue

    assert await worker.process_once("worker-1")

    assert queue.retry_ats == [retry_at]
    assert queue.failures and queue.failures[0][1] is True
    assert plans.plans["plan-1"].status is PlanStatus.APPROVED
    assert provider.branch_creations == []
    resolver = cast(FakeProviderResolver, worker.provider_resolver)
    assert resolver.checks == []


@pytest.mark.asyncio
async def test_other_transient_errors_keep_the_queue_backoff():
    from greenkube.automation.git.base import GitProviderTransientError

    worker, operations, _, _, _ = _worker(provider_error=GitProviderTransientError("503", status_code=503))
    queue = RetryAtOperationRepository(*operations.leases)
    worker.operations = queue

    assert await worker.process_once("worker-1")

    assert queue.retry_ats == [None]
    assert queue.failures[0][1] is True
