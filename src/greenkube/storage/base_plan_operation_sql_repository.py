"""Shared SQL implementation for SQLite and PostgreSQL plan-operation queues."""

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from greenkube.gitops.contracts import Operation, OperationStatus, Plan
from greenkube.storage.base_plan_operation_repository import (
    PlanOperationLease,
    PlanOperationLeaseError,
    PlanOperationRepository,
)
from greenkube.storage.gitops_mapper import database_datetime, datetime_value
from greenkube.storage.plan_operation_mapper import row_to_plan_operation


class BaseSQLPlanOperationRepository(PlanOperationRepository):
    """Backend-neutral queue behavior with dialect-aware transactions and claims."""

    backend: str

    def __init__(self, db_manager: Any) -> None:
        if db_manager.db_type != self.backend:
            raise ValueError(f"{type(self).__name__} requires a {self.backend} database")
        self.db_manager = db_manager
        if self.backend == "sqlite":
            lock = getattr(db_manager, "_plan_operation_transaction_lock", None)
            if lock is None:
                lock = asyncio.Lock()
                setattr(db_manager, "_plan_operation_transaction_lock", lock)
            self._sqlite_lock = lock
        else:
            self._sqlite_lock = None

    @property
    def _postgres(self) -> bool:
        return self.backend == "postgres"

    def _compile_sql(self, query: str) -> str:
        if not self._postgres:
            return query
        pieces = query.split("?")
        if len(pieces) == 1:
            return query
        compiled = pieces[0]
        for index, piece in enumerate(pieces[1:], start=1):
            compiled += f"${index}{piece}"
        return compiled

    async def _fetchrow(
        self,
        connection: Any,
        query: str,
        params: Sequence[Any] = (),
    ) -> Any | None:
        sql = self._compile_sql(query)
        if self._postgres:
            return await connection.fetchrow(sql, *params)
        cursor = await connection.execute(sql, params)
        return await cursor.fetchone()

    async def _execute(
        self,
        connection: Any,
        query: str,
        params: Sequence[Any] = (),
    ) -> int:
        sql = self._compile_sql(query)
        if self._postgres:
            result = await connection.execute(sql, *params)
            try:
                return int(result.rsplit(" ", 1)[-1])
            except (AttributeError, ValueError):
                return 0
        cursor = await connection.execute(sql, params)
        return int(cursor.rowcount)

    @asynccontextmanager
    async def _connection(self) -> AsyncGenerator[Any, None]:
        if self._postgres:
            async with self.db_manager.connection_scope() as connection:
                yield connection
            return
        async with self._sqlite_lock:
            async with self.db_manager.connection_scope() as connection:
                yield connection

    @asynccontextmanager
    async def _transaction(self, *, immediate: bool = False) -> AsyncGenerator[Any, None]:
        if self._postgres:
            async with self.db_manager.connection_scope() as connection:
                async with connection.transaction():
                    yield connection
            return

        async with self._sqlite_lock:
            async with self.db_manager.connection_scope() as connection:
                await connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                try:
                    yield connection
                except BaseException:
                    await connection.rollback()
                    raise
                else:
                    await connection.commit()

    def _db_datetime(self, value: datetime) -> datetime | str:
        normalized = database_datetime(value, postgres=self._postgres)
        assert normalized is not None
        return normalized

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _validate_lease_seconds(lease_seconds: int) -> None:
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds <= 0
            or lease_seconds > 2_147_483_647
        ):
            raise ValueError("lease_seconds must be an integer between 1 and 2147483647")

    @staticmethod
    def _validate_retry_policy(max_attempts: int, backoff_base_seconds: int, backoff_max_seconds: int) -> None:
        for name, value, minimum in (
            ("max_attempts", max_attempts, 1),
            ("backoff_base_seconds", backoff_base_seconds, 0),
            ("backoff_max_seconds", backoff_max_seconds, 0),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > 2_147_483_647:
                raise ValueError(f"{name} must be an integer between {minimum} and 2147483647")
        if backoff_max_seconds < backoff_base_seconds:
            raise ValueError("backoff_max_seconds must be greater than or equal to backoff_base_seconds")

    @staticmethod
    def _retry_delay(attempts: int, base_seconds: int, maximum_seconds: int) -> int:
        delay = min(base_seconds, maximum_seconds)
        if delay == 0:
            return 0
        for _ in range(max(attempts - 1, 0)):
            if delay >= maximum_seconds:
                break
            delay = min(delay * 2, maximum_seconds)
        return delay

    async def enqueue(
        self,
        plan: Plan,
        *,
        idempotency_key: str,
        max_attempts: int = 3,
        backoff_base_seconds: int = 30,
        backoff_max_seconds: int = 3600,
    ) -> Operation:
        self._validate_retry_policy(max_attempts, backoff_base_seconds, backoff_max_seconds)
        if plan.digest is None:
            raise ValueError("a plan operation requires a persisted plan digest")
        expected_key = f"plan:{plan.plan_id}:{plan.digest}"
        if idempotency_key != expected_key:
            raise ValueError("idempotency_key must be derived from the plan ID and digest")

        now = self._now()
        operation_id = str(uuid4())
        async with self._transaction(immediate=True) as connection:
            persisted_plan = await self._fetchrow(
                connection,
                "SELECT digest FROM automation_plans WHERE plan_id = ?",
                (plan.plan_id,),
            )
            if persisted_plan is None:
                raise ValueError(f"plan {plan.plan_id!r} must be persisted before it can be enqueued")
            if persisted_plan["digest"] != plan.digest:
                raise ValueError("persisted plan digest does not match the operation digest")

            row = await self._fetchrow(
                connection,
                """
                INSERT INTO automation_plan_operations
                    (operation_id, plan_id, plan_digest, idempotency_key, status, attempts,
                     max_attempts, backoff_base_seconds, backoff_max_seconds, available_at,
                     provider_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                (
                    operation_id,
                    plan.plan_id,
                    plan.digest,
                    idempotency_key,
                    max_attempts,
                    backoff_base_seconds,
                    backoff_max_seconds,
                    self._db_datetime(now),
                    plan.source.provider_id,
                    self._db_datetime(now),
                    self._db_datetime(now),
                ),
            )
            if row is None:
                row = await self._fetchrow(
                    connection,
                    """
                    SELECT * FROM automation_plan_operations
                    WHERE plan_id = ? OR idempotency_key = ?
                    """,
                    (plan.plan_id, idempotency_key),
                )
                if row is None:
                    raise RuntimeError("plan operation insert conflicted without a matching existing operation")
                if (
                    row["plan_id"] != plan.plan_id
                    or row["plan_digest"] != plan.digest
                    or row["idempotency_key"] != idempotency_key
                ):
                    raise ValueError(f"plan {plan.plan_id!r} already has an operation for a different digest")
            return row_to_plan_operation(row)

    async def get_by_operation_id(self, operation_id: str) -> Operation | None:
        return await self._get("operation_id = ?", operation_id)

    async def get_by_plan_id(self, plan_id: str) -> Operation | None:
        return await self._get("plan_id = ?", plan_id)

    async def get_by_idempotency_key(self, idempotency_key: str) -> Operation | None:
        return await self._get("idempotency_key = ?", idempotency_key)

    async def _get(self, clause: str, value: str) -> Operation | None:
        async with self._connection() as connection:
            row = await self._fetchrow(
                connection,
                f"SELECT * FROM automation_plan_operations WHERE {clause}",
                (value,),
            )
            return row_to_plan_operation(row) if row is not None else None

    async def claim_next(self, worker_id: str, *, lease_seconds: int = 900) -> PlanOperationLease | None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be blank")
        self._validate_lease_seconds(lease_seconds)
        now = self._now()
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        lease_id = str(uuid4())

        async with self._transaction(immediate=True) as connection:
            heartbeat = await self._lock_worker_heartbeat(connection, worker_id, now)
            if heartbeat is None or self._worker_has_active_lease(heartbeat, now):
                return None

            await self._execute(
                connection,
                """
                UPDATE automation_plan_operations
                SET status = 'failed',
                    worker_id = NULL,
                    lease_id = NULL,
                    lease_expires_at = NULL,
                    locked_at = NULL,
                    last_error = COALESCE(last_error, 'worker lease expired after maximum attempts'),
                    updated_at = ?
                WHERE status = 'running'
                  AND lease_expires_at <= ?
                  AND attempts >= max_attempts
                """,
                (self._db_datetime(now), self._db_datetime(now)),
            )

            query = """
                SELECT * FROM automation_plan_operations
                WHERE attempts < max_attempts
                  AND (
                      (status IN ('queued', 'retrying') AND available_at <= ?)
                      OR (status = 'running' AND lease_expires_at <= ?)
                  )
                ORDER BY available_at, created_at, operation_id
                LIMIT 1
            """
            if self._postgres:
                query += " FOR UPDATE SKIP LOCKED"
            row = await self._fetchrow(
                connection,
                query,
                (self._db_datetime(now), self._db_datetime(now)),
            )
            if row is None:
                return None

            updated = await self._execute(
                connection,
                """
                UPDATE automation_plan_operations
                SET status = 'running',
                    attempts = attempts + 1,
                    locked_at = ?,
                    worker_id = ?,
                    lease_id = ?,
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE operation_id = ?
                """,
                (
                    self._db_datetime(now),
                    worker_id,
                    lease_id,
                    self._db_datetime(lease_expires_at),
                    self._db_datetime(now),
                    row["operation_id"],
                ),
            )
            if updated != 1:
                raise RuntimeError("selected plan operation could not be claimed")

            heartbeat_updated = await self._execute(
                connection,
                """
                UPDATE automation_worker_heartbeats
                SET status = 'running',
                    heartbeat_at = ?,
                    lease_id = ?,
                    lease_owner = ?,
                    lease_expires_at = ?,
                    lease_plan_operation_id = ?,
                    updated_at = ?
                WHERE worker_id = ?
                """,
                (
                    self._db_datetime(now),
                    lease_id,
                    worker_id,
                    self._db_datetime(lease_expires_at),
                    row["operation_id"],
                    self._db_datetime(now),
                    worker_id,
                ),
            )
            if heartbeat_updated != 1:
                raise RuntimeError("worker heartbeat disappeared while claiming a plan operation")

            claimed_row = await self._fetchrow(
                connection,
                "SELECT * FROM automation_plan_operations WHERE operation_id = ?",
                (row["operation_id"],),
            )
            assert claimed_row is not None
            return PlanOperationLease(
                operation=row_to_plan_operation(claimed_row),
                lease_id=lease_id,
                lease_expires_at=lease_expires_at,
            )

    async def _lock_worker_heartbeat(self, connection: Any, worker_id: str, now: datetime) -> Any | None:
        await self._execute(
            connection,
            """
            INSERT INTO automation_worker_heartbeats (worker_id, status, heartbeat_at, updated_at)
            VALUES (?, 'idle', ?, ?)
            ON CONFLICT (worker_id) DO NOTHING
            """,
            (worker_id, self._db_datetime(now), self._db_datetime(now)),
        )
        query = "SELECT * FROM automation_worker_heartbeats WHERE worker_id = ?"
        if self._postgres:
            query += " FOR UPDATE"
        return await self._fetchrow(connection, query, (worker_id,))

    @staticmethod
    def _worker_has_active_lease(heartbeat: Any, now: datetime) -> bool:
        expires_at = datetime_value(heartbeat["lease_expires_at"])
        if expires_at is None or expires_at <= now:
            return False
        return (
            heartbeat["lease_id"] is not None
            or heartbeat["lease_operation_id"] is not None
            or heartbeat["lease_plan_operation_id"] is not None
            or heartbeat["lease_owner"] is not None
        )

    async def heartbeat(self, lease: PlanOperationLease, *, lease_seconds: int = 900) -> PlanOperationLease:
        self._validate_lease_seconds(lease_seconds)
        worker_id = self._lease_worker_id(lease)
        operation_id = lease.operation.operation_id
        now = self._now()
        lease_expires_at = now + timedelta(seconds=lease_seconds)

        async with self._transaction(immediate=True) as connection:
            updated = await self._execute(
                connection,
                """
                UPDATE automation_plan_operations
                SET lease_expires_at = ?, updated_at = ?
                WHERE operation_id = ?
                  AND status = 'running'
                  AND worker_id = ?
                  AND lease_id = ?
                  AND lease_expires_at > ?
                """,
                (
                    self._db_datetime(lease_expires_at),
                    self._db_datetime(now),
                    operation_id,
                    worker_id,
                    lease.lease_id,
                    self._db_datetime(now),
                ),
            )
            if updated != 1:
                raise PlanOperationLeaseError("plan operation lease is no longer owned by this worker")

            heartbeat_updated = await self._execute(
                connection,
                """
                UPDATE automation_worker_heartbeats
                SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                WHERE worker_id = ?
                  AND lease_plan_operation_id = ?
                  AND lease_id = ?
                  AND lease_owner = ?
                  AND lease_expires_at > ?
                """,
                (
                    self._db_datetime(now),
                    self._db_datetime(lease_expires_at),
                    self._db_datetime(now),
                    worker_id,
                    operation_id,
                    lease.lease_id,
                    worker_id,
                    self._db_datetime(now),
                ),
            )
            if heartbeat_updated != 1:
                raise PlanOperationLeaseError("worker heartbeat no longer owns the plan operation lease")
            row = await self._fetchrow(
                connection,
                "SELECT * FROM automation_plan_operations WHERE operation_id = ?",
                (operation_id,),
            )
            assert row is not None
            return PlanOperationLease(
                operation=row_to_plan_operation(row),
                lease_id=lease.lease_id,
                lease_expires_at=lease_expires_at,
            )

    async def mark_succeeded(
        self,
        lease: PlanOperationLease,
        *,
        provider_id: str | None = None,
        commit_sha: str | None = None,
        pull_request_number: int | None = None,
    ) -> Operation:
        worker_id = self._lease_worker_id(lease)
        operation_id = lease.operation.operation_id
        now = self._now()

        async with self._transaction(immediate=True) as connection:
            updated = await self._execute(
                connection,
                """
                UPDATE automation_plan_operations
                SET status = 'succeeded',
                    provider_id = COALESCE(?, provider_id),
                    commit_sha = COALESCE(?, commit_sha),
                    pull_request_number = COALESCE(?, pull_request_number),
                    worker_id = NULL,
                    lease_id = NULL,
                    lease_expires_at = NULL,
                    locked_at = NULL,
                    last_error = NULL,
                    updated_at = ?
                WHERE operation_id = ?
                  AND status = 'running'
                  AND worker_id = ?
                  AND lease_id = ?
                  AND lease_expires_at > ?
                """,
                (
                    provider_id,
                    commit_sha,
                    pull_request_number,
                    self._db_datetime(now),
                    operation_id,
                    worker_id,
                    lease.lease_id,
                    self._db_datetime(now),
                ),
            )
            if updated != 1:
                raise PlanOperationLeaseError("plan operation lease is no longer owned by this worker")
            await self._release_worker_heartbeat(connection, lease, now)
            row = await self._fetchrow(
                connection,
                "SELECT * FROM automation_plan_operations WHERE operation_id = ?",
                (operation_id,),
            )
            assert row is not None
            return row_to_plan_operation(row)

    async def mark_failed(self, lease: PlanOperationLease, error: str, *, retryable: bool = True) -> Operation:
        if not error.strip():
            raise ValueError("error must not be blank")
        worker_id = self._lease_worker_id(lease)
        operation_id = lease.operation.operation_id
        now = self._now()

        async with self._transaction(immediate=True) as connection:
            query = """
                SELECT * FROM automation_plan_operations
                WHERE operation_id = ?
                  AND status = 'running'
                  AND worker_id = ?
                  AND lease_id = ?
                  AND lease_expires_at > ?
            """
            if self._postgres:
                query += " FOR UPDATE"
            row = await self._fetchrow(
                connection,
                query,
                (operation_id, worker_id, lease.lease_id, self._db_datetime(now)),
            )
            if row is None:
                raise PlanOperationLeaseError("plan operation lease is no longer owned by this worker")

            should_retry = retryable and int(row["attempts"]) < int(row["max_attempts"])
            status = OperationStatus.RETRYING.value if should_retry else OperationStatus.FAILED.value
            available_at = self._db_datetime(now)
            if should_retry:
                retry_delay = self._retry_delay(
                    int(row["attempts"]),
                    int(row["backoff_base_seconds"]),
                    int(row["backoff_max_seconds"]),
                )
                available_at = self._db_datetime(now + timedelta(seconds=retry_delay))

            updated = await self._execute(
                connection,
                """
                UPDATE automation_plan_operations
                SET status = ?,
                    available_at = ?,
                    worker_id = NULL,
                    lease_id = NULL,
                    lease_expires_at = NULL,
                    locked_at = NULL,
                    last_error = ?,
                    updated_at = ?
                WHERE operation_id = ?
                  AND status = 'running'
                  AND worker_id = ?
                  AND lease_id = ?
                """,
                (
                    status,
                    available_at,
                    error.strip(),
                    self._db_datetime(now),
                    operation_id,
                    worker_id,
                    lease.lease_id,
                ),
            )
            if updated != 1:
                raise PlanOperationLeaseError("plan operation lease is no longer owned by this worker")
            await self._release_worker_heartbeat(connection, lease, now)
            updated_row = await self._fetchrow(
                connection,
                "SELECT * FROM automation_plan_operations WHERE operation_id = ?",
                (operation_id,),
            )
            assert updated_row is not None
            return row_to_plan_operation(updated_row)

    def _lease_worker_id(self, lease: PlanOperationLease) -> str:
        worker_id = lease.operation.worker_id
        if worker_id is None:
            raise PlanOperationLeaseError("plan operation lease has no worker owner")
        return worker_id

    async def _release_worker_heartbeat(self, connection: Any, lease: PlanOperationLease, now: datetime) -> None:
        worker_id = self._lease_worker_id(lease)
        released = await self._execute(
            connection,
            """
            UPDATE automation_worker_heartbeats
            SET status = 'idle',
                heartbeat_at = ?,
                lease_id = NULL,
                lease_owner = NULL,
                lease_expires_at = NULL,
                lease_plan_operation_id = NULL,
                updated_at = ?
            WHERE worker_id = ?
              AND lease_plan_operation_id = ?
              AND lease_id = ?
              AND lease_owner = ?
            """,
            (
                self._db_datetime(now),
                self._db_datetime(now),
                worker_id,
                lease.operation.operation_id,
                lease.lease_id,
                worker_id,
            ),
        )
        if released != 1:
            raise PlanOperationLeaseError("worker heartbeat no longer owns the plan operation lease")
