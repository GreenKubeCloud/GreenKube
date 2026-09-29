"""Repository contract for durable automation operations."""

from abc import ABC, abstractmethod
from typing import Optional

from greenkube.automation.operations import AutomationOperation


class AutomationOperationRepository(ABC):
    @abstractmethod
    async def enqueue(self, operation: AutomationOperation) -> AutomationOperation: ...

    @abstractmethod
    async def get_by_id(self, operation_id: int) -> Optional[AutomationOperation]: ...

    @abstractmethod
    async def get_by_idempotency_key(self, key: str) -> Optional[AutomationOperation]: ...

    @abstractmethod
    async def claim_next(self, worker_id: str, stale_after_seconds: int = 900) -> Optional[AutomationOperation]: ...

    @abstractmethod
    async def mark_succeeded(
        self, operation_id: int, *, result: dict, preview_digest: str, commit_digest: Optional[str]
    ) -> AutomationOperation: ...

    @abstractmethod
    async def mark_failed(self, operation_id: int, error: str) -> AutomationOperation: ...

    @abstractmethod
    async def reschedule(self, operation_id: int, error: str, available_at) -> AutomationOperation: ...

    @abstractmethod
    async def reconcile(self, stale_after_seconds: int = 900) -> int: ...
