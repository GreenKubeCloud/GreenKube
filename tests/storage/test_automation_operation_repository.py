"""Tests for the durable automation operation queue."""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.automation.operations import AutomationOperation
from greenkube.core.db import db_manager
from greenkube.storage.sqlite.automation_operation_repository import SQLiteAutomationOperationRepository


@pytest.fixture
async def repository():
    await db_manager.setup_sqlite(db_path=":memory:")
    yield SQLiteAutomationOperationRepository(db_manager)
    await db_manager.close()


@pytest.mark.asyncio
async def test_claim_and_complete_preserves_digests(repository):
    operation = await repository.enqueue(
        AutomationOperation(None, 12, "request-1", "fingerprint-1", {"dry_run": False}, actor="ci")
    )
    assert await repository.get_by_idempotency_key("request-1") == operation

    claimed = await repository.claim_next("worker-1")
    assert claimed is not None
    assert claimed.attempts == 1

    completed = await repository.mark_succeeded(
        claimed.id, result={"status": "pr_open"}, preview_digest="preview-1", commit_digest="commit-1"
    )
    assert completed.status.value == "succeeded"
    assert completed.preview_digest == "preview-1"
    assert completed.commit_digest == "commit-1"

    with pytest.raises(ValueError, match="immutable"):
        await repository.mark_succeeded(
            claimed.id, result={"status": "pr_open"}, preview_digest="different", commit_digest="commit-1"
        )


@pytest.mark.asyncio
async def test_reconcile_requeues_stale_running_operation(repository):
    operation = await repository.enqueue(
        AutomationOperation(None, 13, "request-2", "fingerprint-2", {"dry_run": False})
    )
    claimed = await repository.claim_next("worker-1")
    assert claimed is not None

    async with db_manager.connection_scope() as conn:
        stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        await conn.execute("UPDATE automation_operations SET locked_at = ? WHERE id = ?", (stale, operation.id))
        await conn.commit()

    assert await repository.reconcile(stale_after_seconds=60) == 1
    reclaimed = await repository.claim_next("worker-2")
    assert reclaimed is not None
    assert reclaimed.id == operation.id
    assert reclaimed.attempts == 2


@pytest.mark.asyncio
async def test_reschedule_preserves_attempt_and_delays_claim(repository):
    await repository.enqueue(AutomationOperation(None, 14, "request-3", "fingerprint-3", {"dry_run": False}))
    claimed = await repository.claim_next("worker-1")
    retry_at = datetime.now(timezone.utc) + timedelta(minutes=5)

    retried = await repository.reschedule(claimed.id, "provider unavailable", retry_at)

    assert retried.status.value == "queued"
    assert retried.attempts == 1
    assert retried.error == "provider unavailable"
    assert retried.available_at is not None
    assert retried.available_at >= retry_at - timedelta(seconds=1)
    assert await repository.claim_next("worker-2") is None
