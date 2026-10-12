"""Repository contract for durable, digest-addressed plan operations."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from greenkube.gitops.contracts import Operation, Plan


@dataclass(frozen=True, slots=True)
class PlanOperationLease:
    """An operation claim paired with the fencing token required to mutate it."""

    operation: Operation
    lease_id: str
    lease_expires_at: datetime


class PlanOperationLeaseError(ValueError):
    """Raised when a worker attempts to use a lease it no longer owns."""


class PlanOperationRepository(ABC):
    """Persistence port for digest-idempotent plan-operation queues."""

    @abstractmethod
    async def enqueue(
        self,
        plan: Plan,
        *,
        idempotency_key: str,
        max_attempts: int = 3,
        backoff_base_seconds: int = 30,
        backoff_max_seconds: int = 3600,
    ) -> Operation: ...

    @abstractmethod
    async def get_by_operation_id(self, operation_id: str) -> Operation | None: ...

    @abstractmethod
    async def get_by_plan_id(self, plan_id: str) -> Operation | None: ...

    @abstractmethod
    async def get_by_idempotency_key(self, idempotency_key: str) -> Operation | None: ...

    @abstractmethod
    async def claim_next(self, worker_id: str, *, lease_seconds: int = 900) -> PlanOperationLease | None: ...

    @abstractmethod
    async def heartbeat(self, lease: PlanOperationLease, *, lease_seconds: int = 900) -> PlanOperationLease: ...

    @abstractmethod
    async def mark_succeeded(
        self,
        lease: PlanOperationLease,
        *,
        provider_id: str | None = None,
        commit_sha: str | None = None,
        pull_request_number: int | None = None,
    ) -> Operation: ...

    @abstractmethod
    async def mark_failed(self, lease: PlanOperationLease, error: str, *, retryable: bool = True) -> Operation: ...
