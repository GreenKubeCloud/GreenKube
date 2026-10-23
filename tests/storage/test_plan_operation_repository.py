"""Contract tests for the durable plan-operation queue."""

import asyncio
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.gitops.contracts import (
    ActionPlanV3,
    AddHorizontalPodAutoscaler,
    BindingStatus,
    ControllerKind,
    OperationStatus,
    Plan,
    RendererKind,
    ResourceTarget,
    WorkloadSource,
)
from greenkube.storage.base_plan_operation_repository import PlanOperationLeaseError
from greenkube.storage.postgres.gitops_repository import PostgresGitOpsRepository
from greenkube.storage.postgres.plan_operation_repository import PostgresPlanOperationRepository
from greenkube.storage.sqlite.gitops_repository import SQLiteGitOpsRepository
from greenkube.storage.sqlite.plan_operation_repository import SQLitePlanOperationRepository


@pytest.fixture(params=("sqlite", "postgres"), ids=("sqlite", "postgres"))
async def plan_operation_store(request, monkeypatch):
    backend = request.param
    postgres_dsn = None
    schema = None

    if backend == "postgres":
        postgres_dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if postgres_dsn is None:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL plan-operation contract tests.")
        schema = f"gk_plan_operations_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", postgres_dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", ":memory:")
        monkeypatch.setenv("DB_SCHEMA", "public")

    monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    config.reload()
    manager = DatabaseManager(Config())

    try:
        await manager.connect()
        repository = (
            PostgresPlanOperationRepository(manager)
            if backend == "postgres"
            else SQLitePlanOperationRepository(manager)
        )
        gitops_repository = (
            PostgresGitOpsRepository(manager) if backend == "postgres" else SQLiteGitOpsRepository(manager)
        )
        yield repository, gitops_repository, manager, backend
    finally:
        await manager.close()
        if postgres_dsn is not None and schema is not None:
            connection = await asyncpg.connect(postgres_dsn)
            try:
                await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            finally:
                await connection.close()


def _plan(plan_id: str = "plan-one", digest: str | None = None) -> Plan:
    plan_digest = digest or f"sha256:{plan_id.encode().hex():0<64}"[:71]
    source = WorkloadSource(
        id=f"source-{plan_id}",
        cluster="cluster-a",
        namespace="production",
        workload_kind="Deployment",
        workload_name="api",
        provider_id="provider-github",
        repo_url="https://github.com/acme/platform",
        ref="refs/heads/main",
        path="apps/api",
        controller_kind=ControllerKind.EXPLICIT_REPOSITORY,
        controller_name="api",
        controller_namespace="production",
        renderer=RendererKind.RAW,
        confidence=0.95,
        status=BindingStatus.RESOLVED,
    )
    action = ActionPlanV3(
        action_id=plan_id,
        recommendation_ids=[101],
        target=ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="production", name="api"),
        operations=[AddHorizontalPodAutoscaler(min_replicas=2, max_replicas=4)],
        evidence_ref="recommendations:101",
    )
    return Plan(
        plan_id=plan_id,
        recommendation_ids=[101],
        source=source,
        action=action,
        digest=plan_digest,
    )


async def _persist_plan(manager: DatabaseManager, backend: str, plan: Plan) -> None:
    async with manager.connection_scope() as connection:
        if backend == "postgres":
            await connection.execute(
                "INSERT INTO automation_plans (plan_id, digest) VALUES ($1, $2)",
                plan.plan_id,
                plan.digest,
            )
        else:
            await connection.execute(
                "INSERT INTO automation_plans (plan_id, digest) VALUES (?, ?)",
                (plan.plan_id, plan.digest),
            )
            await connection.commit()


async def _force_available_at(
    manager: DatabaseManager, backend: str, operation_id: str, available_at: datetime
) -> None:
    async with manager.connection_scope() as connection:
        if backend == "postgres":
            await connection.execute(
                "UPDATE automation_plan_operations SET available_at = $1 WHERE operation_id = $2",
                available_at,
                operation_id,
            )
        else:
            await connection.execute(
                "UPDATE automation_plan_operations SET available_at = ? WHERE operation_id = ?",
                (available_at.isoformat(), operation_id),
            )
            await connection.commit()


async def _update_plan_digest(manager: DatabaseManager, backend: str, plan_id: str, digest: str) -> None:
    async with manager.connection_scope() as connection:
        if backend == "postgres":
            await connection.execute("UPDATE automation_plans SET digest = $1 WHERE plan_id = $2", digest, plan_id)
        else:
            await connection.execute("UPDATE automation_plans SET digest = ? WHERE plan_id = ?", (digest, plan_id))
            await connection.commit()


async def _expire_lease(manager: DatabaseManager, backend: str, operation_id: str) -> None:
    expired_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    async with manager.connection_scope() as connection:
        if backend == "postgres":
            await connection.execute(
                "UPDATE automation_plan_operations SET lease_expires_at = $1 WHERE operation_id = $2",
                expired_at,
                operation_id,
            )
            await connection.execute(
                "UPDATE automation_worker_heartbeats SET lease_expires_at = $1 WHERE lease_plan_operation_id = $2",
                expired_at,
                operation_id,
            )
        else:
            expired_text = expired_at.isoformat()
            await connection.execute(
                "UPDATE automation_plan_operations SET lease_expires_at = ? WHERE operation_id = ?",
                (expired_text, operation_id),
            )
            await connection.execute(
                "UPDATE automation_worker_heartbeats SET lease_expires_at = ? WHERE lease_plan_operation_id = ?",
                (expired_text, operation_id),
            )
            await connection.commit()


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_per_plan_and_digest(plan_operation_store):
    repository, _, manager, backend = plan_operation_store
    plan = _plan()
    await _persist_plan(manager, backend, plan)
    idempotency_key = f"plan:{plan.plan_id}:{plan.digest}"

    enqueued = await repository.enqueue(plan, idempotency_key=idempotency_key)
    duplicate = await repository.enqueue(plan, idempotency_key=idempotency_key)

    assert duplicate == enqueued
    assert duplicate.idempotency_key == idempotency_key
    assert duplicate.plan_id == plan.plan_id
    assert await repository.get_by_operation_id(enqueued.operation_id) == enqueued
    assert await repository.get_by_plan_id(plan.plan_id) == enqueued
    assert await repository.get_by_idempotency_key(idempotency_key) == enqueued

    with pytest.raises(ValueError, match="derived from the plan ID and digest"):
        await repository.enqueue(plan, idempotency_key="not-derived-from-the-plan")

    changed_plan = _plan(plan_id=plan.plan_id, digest=f"sha256:{'f' * 64}")
    assert changed_plan.digest is not None
    await _update_plan_digest(manager, backend, changed_plan.plan_id, changed_plan.digest)
    with pytest.raises(ValueError, match="already has an operation"):
        await repository.enqueue(
            changed_plan,
            idempotency_key=f"plan:{changed_plan.plan_id}:{changed_plan.digest}",
        )


@pytest.mark.asyncio
async def test_claim_is_exclusive_and_updates_worker_heartbeat(plan_operation_store):
    repository, gitops_repository, manager, backend = plan_operation_store
    plan = _plan()
    await _persist_plan(manager, backend, plan)
    queued = await repository.enqueue(plan, idempotency_key=f"plan:{plan.plan_id}:{plan.digest}")

    first, second = await asyncio.gather(
        repository.claim_next("worker-a", lease_seconds=60),
        repository.claim_next("worker-b", lease_seconds=60),
    )

    leases = [lease for lease in (first, second) if lease is not None]
    assert len(leases) == 1
    lease = leases[0]
    assert lease.operation.operation_id == queued.operation_id
    assert lease.operation.status is OperationStatus.RUNNING
    assert lease.operation.attempts == 1
    assert lease.operation.worker_id in {"worker-a", "worker-b"}
    assert lease.lease_id
    assert lease.lease_expires_at > datetime.now(timezone.utc)
    assert await repository.claim_next("worker-c") is None

    heartbeat = await gitops_repository.get_worker_heartbeat(lease.operation.worker_id)
    assert heartbeat is not None
    assert heartbeat.lease_id == lease.lease_id
    assert heartbeat.lease_owner == lease.operation.worker_id
    assert heartbeat.lease_plan_operation_id == lease.operation.operation_id

    completed = await repository.mark_succeeded(
        lease,
        provider_id="provider-github",
        commit_sha="abc123",
        pull_request_number=42,
    )
    assert completed.status is OperationStatus.SUCCEEDED
    assert completed.worker_id is None
    assert completed.provider_id == "provider-github"
    assert completed.commit_sha == "abc123"
    assert completed.pull_request_number == 42

    released_heartbeat = await gitops_repository.get_worker_heartbeat(lease.operation.worker_id)
    assert released_heartbeat is not None
    assert released_heartbeat.lease_plan_operation_id is None
    assert released_heartbeat.lease_id is None


@pytest.mark.asyncio
async def test_stale_lease_is_reclaimed_and_old_owner_cannot_finish(plan_operation_store):
    repository, _, manager, backend = plan_operation_store
    plan = _plan()
    await _persist_plan(manager, backend, plan)
    await repository.enqueue(plan, idempotency_key=f"plan:{plan.plan_id}:{plan.digest}")

    first_lease = await repository.claim_next("worker-a", lease_seconds=60)
    assert first_lease is not None
    await _expire_lease(manager, backend, first_lease.operation.operation_id)

    reclaimed_lease = await repository.claim_next("worker-b", lease_seconds=60)

    assert reclaimed_lease is not None
    assert reclaimed_lease.operation.operation_id == first_lease.operation.operation_id
    assert reclaimed_lease.operation.attempts == 2
    assert reclaimed_lease.operation.worker_id == "worker-b"
    assert reclaimed_lease.lease_id != first_lease.lease_id
    with pytest.raises(PlanOperationLeaseError, match="lease"):
        await repository.mark_succeeded(first_lease)


@pytest.mark.asyncio
async def test_failed_operations_use_bounded_exponential_backoff(plan_operation_store):
    repository, _, manager, backend = plan_operation_store
    plan = _plan()
    await _persist_plan(manager, backend, plan)
    queued = await repository.enqueue(
        plan,
        idempotency_key=f"plan:{plan.plan_id}:{plan.digest}",
        max_attempts=3,
        backoff_base_seconds=30,
        backoff_max_seconds=45,
    )

    first_lease = await repository.claim_next("worker-a", lease_seconds=60)
    assert first_lease is not None
    first_failed_at = datetime.now(timezone.utc)
    first_retry = await repository.mark_failed(first_lease, "provider unavailable")

    assert first_retry.status is OperationStatus.RETRYING
    assert first_retry.attempts == 1
    assert first_retry.last_error == "provider unavailable"
    assert first_retry.available_at is not None
    assert timedelta(seconds=29) <= first_retry.available_at - first_failed_at <= timedelta(seconds=32)
    assert await repository.claim_next("worker-b") is None

    await _force_available_at(
        manager,
        backend,
        queued.operation_id,
        datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    second_lease = await repository.claim_next("worker-b", lease_seconds=60)
    assert second_lease is not None
    assert second_lease.operation.attempts == 2
    second_failed_at = datetime.now(timezone.utc)
    second_retry = await repository.mark_failed(second_lease, "still unavailable")

    assert second_retry.status is OperationStatus.RETRYING
    assert second_retry.available_at is not None
    assert timedelta(seconds=44) <= second_retry.available_at - second_failed_at <= timedelta(seconds=47)

    await _force_available_at(
        manager,
        backend,
        queued.operation_id,
        datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    final_lease = await repository.claim_next("worker-c", lease_seconds=60)
    assert final_lease is not None
    assert final_lease.operation.attempts == 3
    exhausted = await repository.mark_failed(final_lease, "final failure")

    assert exhausted.status is OperationStatus.FAILED
    assert exhausted.attempts == 3
    assert exhausted.last_error == "final failure"
    assert await repository.claim_next("worker-d") is None


@pytest.mark.asyncio
async def test_lease_heartbeat_renews_only_the_current_owner(plan_operation_store):
    repository, gitops_repository, manager, backend = plan_operation_store
    plan = _plan()
    await _persist_plan(manager, backend, plan)
    await repository.enqueue(plan, idempotency_key=f"plan:{plan.plan_id}:{plan.digest}")

    lease = await repository.claim_next("worker-a", lease_seconds=1)
    assert lease is not None
    renewed = await repository.heartbeat(lease, lease_seconds=120)

    assert renewed.lease_id == lease.lease_id
    assert renewed.lease_expires_at > lease.lease_expires_at
    heartbeat = await gitops_repository.get_worker_heartbeat("worker-a")
    assert heartbeat is not None
    assert heartbeat.lease_expires_at == renewed.lease_expires_at

    forged_lease = replace(renewed, lease_id="not-the-current-lease")
    with pytest.raises(PlanOperationLeaseError, match="lease"):
        await repository.heartbeat(forged_lease, lease_seconds=120)

    await _expire_lease(manager, backend, lease.operation.operation_id)
    with pytest.raises(PlanOperationLeaseError, match="lease"):
        await repository.heartbeat(lease, lease_seconds=120)
