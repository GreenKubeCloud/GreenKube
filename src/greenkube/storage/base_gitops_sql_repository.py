"""Shared SQL implementation used by the GitOps backend adapters."""

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from greenkube.gitops.contracts import (
    ActionStatus,
    BindingStatus,
    Plan,
    PlanStatus,
    PreApplyWorkloadBaseline,
    PullRequestEvent,
    WorkloadSource,
    binding_candidate_generation_token,
    configured_binding_ttl,
    sanitize_repository_url,
)
from greenkube.storage.base_gitops_repository import (
    MAX_PULL_REQUEST_POLLING_LEASE_SECONDS,
    MAX_WORKLOAD_BINDING_CANDIDATES,
    AutomationBacklogSummary,
    AutomationPlanRecord,
    GitOpsProviderConfig,
    GitOpsRepository,
    PullRequestPollingLease,
    PullRequestPollingLeaseNotOwnedError,
    WebhookDelivery,
    WorkerHeartbeat,
    WorkloadBinding,
    WorkloadBindingConflictError,
    WorkloadBindingCoverage,
    WorkloadBindingEvent,
    WorkloadBindingEventType,
    WorkloadBindingPage,
    _binding_identity_cursor,
    _binding_identity_from_cursor,
    _validate_binding_page,
    freshen_workload_binding,
    freshen_workload_source,
)
from greenkube.storage.gitops_mapper import (
    PLAN_PRE_APPLY_BASELINE_POLICY_KEY,
    database_datetime,
    datetime_value,
    json_text,
    json_value,
    row_to_automation_plan_record,
    row_to_gitops_provider,
    row_to_webhook_delivery,
    row_to_worker_heartbeat,
    row_to_workload_binding,
    row_to_workload_binding_event,
    row_to_workload_source,
)
from greenkube.storage.unit_of_work import UnitOfWork


class _BindingGenerationConflict(RuntimeError):
    pass


def _validated_audit_actor(actor: str) -> str:
    if not isinstance(actor, str) or not actor or actor != actor.strip() or len(actor) > 128:
        raise ValueError("audit actor must be a non-empty string of at most 128 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in actor):
        raise ValueError("audit actor must not contain control characters")
    return actor


class BaseSQLGitOpsRepository(GitOpsRepository):
    """Backend-neutral GitOps storage behavior with dialect-aware SQL execution."""

    backend: str

    def __init__(self, db_manager: Any):
        self.db_manager = db_manager
        if db_manager.db_type != self.backend:
            raise ValueError(f"{type(self).__name__} requires a {self.backend} database")
        self._polling_lease_lock: asyncio.Lock | None = None
        if self.backend == "sqlite":
            # Compound writes across these repositories share SQLite's single connection.
            lock = getattr(db_manager, "_plan_operation_transaction_lock", None)
            if lock is None:
                lock = asyncio.Lock()
                setattr(db_manager, "_plan_operation_transaction_lock", lock)
            self._polling_lease_lock = lock

    @property
    def _postgres(self) -> bool:
        return self.backend == "postgres"

    @property
    def _now_expression(self) -> str:
        return "NOW()" if self._postgres else "datetime('now')"

    def _compile_sql(self, query: str, json_positions: Sequence[int] = ()) -> str:
        if not self._postgres:
            return query
        pieces = query.split("?")
        if len(pieces) == 1:
            return query
        compiled = pieces[0]
        json_parameter_indexes = set(json_positions)
        for index, piece in enumerate(pieces[1:], start=1):
            cast = "::jsonb" if index in json_parameter_indexes else ""
            compiled += f"${index}{cast}{piece}"
        return compiled

    async def _fetchrow(
        self,
        connection: Any,
        query: str,
        params: Sequence[Any] = (),
        *,
        json_positions: Sequence[int] = (),
    ) -> Any | None:
        sql = self._compile_sql(query, json_positions)
        if self._postgres:
            return await connection.fetchrow(sql, *params)
        cursor = await connection.execute(sql, params)
        return await cursor.fetchone()

    async def _fetch(
        self,
        connection: Any,
        query: str,
        params: Sequence[Any] = (),
        *,
        json_positions: Sequence[int] = (),
    ) -> list[Any]:
        sql = self._compile_sql(query, json_positions)
        if self._postgres:
            return list(await connection.fetch(sql, *params))
        cursor = await connection.execute(sql, params)
        return list(await cursor.fetchall())

    async def _execute(
        self,
        connection: Any,
        query: str,
        params: Sequence[Any] = (),
        *,
        json_positions: Sequence[int] = (),
    ) -> int:
        sql = self._compile_sql(query, json_positions)
        if self._postgres:
            result = await connection.execute(sql, *params)
            try:
                return int(result.rsplit(" ", 1)[-1])
            except (AttributeError, ValueError):
                return 0
        cursor = await connection.execute(sql, params)
        return int(cursor.rowcount)

    @asynccontextmanager
    async def _transaction(self) -> AsyncGenerator[Any, None]:
        async with UnitOfWork(self.db_manager) as unit_of_work:
            assert unit_of_work.connection is not None
            yield unit_of_work.connection

    @asynccontextmanager
    async def _serialized_transaction(self) -> AsyncGenerator[Any, None]:
        if self._polling_lease_lock is None:
            async with self._transaction() as connection:
                yield connection
            return
        async with self._polling_lease_lock:
            async with self._transaction() as connection:
                yield connection

    @asynccontextmanager
    async def _polling_lease_transaction(self) -> AsyncGenerator[Any, None]:
        async with self._serialized_transaction() as connection:
            yield connection

    @asynccontextmanager
    async def _serialized_connection(self) -> AsyncGenerator[Any, None]:
        if self._polling_lease_lock is None:
            async with self.db_manager.connection_scope() as connection:
                yield connection
            return
        async with self._polling_lease_lock:
            async with self.db_manager.connection_scope() as connection:
                yield connection

    def _db_datetime(self, value: datetime | None) -> datetime | str | None:
        return database_datetime(value, postgres=self._postgres)

    async def _record_recommendation_audit_event(
        self,
        connection: Any,
        recommendation_id: int,
        *,
        event_type: str,
        actor: str,
        details: dict[str, Any],
    ) -> None:
        payload = {"version": 2, "details": details}
        await self._execute(
            connection,
            """
            INSERT INTO recommendation_events
                (recommendation_id, event_type, actor, payload, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                recommendation_id,
                event_type,
                actor,
                json_text(payload),
                self._db_datetime(datetime.now(timezone.utc)),
            ),
            json_positions=(4,),
        )

    @property
    def _lease_now_expression(self) -> str:
        return "clock_timestamp()" if self._postgres else "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"

    @property
    def _lease_expiry_expression(self) -> str:
        if self._postgres:
            return "clock_timestamp() + (? * INTERVAL '1 second')"
        return "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '+' || ? || ' seconds')"

    @property
    def _lease_expired_predicate(self) -> str:
        if self._postgres:
            return "gitops_pr_polling_lease.expires_at <= clock_timestamp()"
        return "julianday(gitops_pr_polling_lease.expires_at) <= julianday('now')"

    @property
    def _lease_live_predicate(self) -> str:
        if self._postgres:
            return "expires_at > clock_timestamp()"
        return "julianday(expires_at) > julianday('now')"

    @staticmethod
    def _validate_pr_polling_lease_request(owner_id: str, lease_seconds: int) -> None:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("owner_id must not be blank")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 1 <= lease_seconds <= MAX_PULL_REQUEST_POLLING_LEASE_SECONDS
        ):
            raise ValueError(f"lease_seconds must be an integer between 1 and {MAX_PULL_REQUEST_POLLING_LEASE_SECONDS}")

    @staticmethod
    def _row_to_pr_polling_lease(row: Any) -> PullRequestPollingLease:
        expires_at = datetime_value(row["expires_at"])
        if expires_at is None:
            raise RuntimeError("PR polling lease row has no expiration timestamp")
        return PullRequestPollingLease(
            owner_id=str(row["owner_id"]),
            fencing_token=int(row["fencing_token"]),
            expires_at=expires_at,
        )

    async def save_provider_config(self, provider: GitOpsProviderConfig) -> GitOpsProviderConfig:
        updated_at = datetime.now(timezone.utc)
        async with self._transaction() as connection:
            await self._execute(
                connection,
                """
                INSERT INTO gitops_providers
                    (id, provider_type, api_base_url, secret_namespace, secret_name, secret_key,
                     enabled, capabilities, status, last_checked_at, last_error_code, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    provider_type = excluded.provider_type,
                    api_base_url = excluded.api_base_url,
                    secret_namespace = excluded.secret_namespace,
                    secret_name = excluded.secret_name,
                    secret_key = excluded.secret_key,
                    enabled = excluded.enabled,
                    capabilities = excluded.capabilities,
                    status = excluded.status,
                    last_checked_at = excluded.last_checked_at,
                    last_error_code = excluded.last_error_code,
                    updated_at = excluded.updated_at
                """,
                (
                    provider.id,
                    provider.provider_type,
                    provider.api_base_url,
                    provider.secret_namespace,
                    provider.secret_name,
                    provider.secret_key,
                    provider.enabled,
                    json_text(provider.capabilities),
                    provider.status,
                    self._db_datetime(provider.last_checked_at),
                    provider.last_error_code,
                    self._db_datetime(updated_at),
                ),
                json_positions=(8,),
            )
            row = await self._fetchrow(connection, "SELECT * FROM gitops_providers WHERE id = ?", (provider.id,))
            assert row is not None
            return row_to_gitops_provider(row)

    async def get_provider_config(self, provider_id: str) -> GitOpsProviderConfig | None:
        async with self.db_manager.connection_scope() as connection:
            row = await self._fetchrow(connection, "SELECT * FROM gitops_providers WHERE id = ?", (provider_id,))
            return row_to_gitops_provider(row) if row is not None else None

    async def list_provider_configs(self, *, enabled_only: bool = False) -> list[GitOpsProviderConfig]:
        query = "SELECT * FROM gitops_providers"
        params: tuple[Any, ...] = ()
        if enabled_only:
            query += " WHERE enabled = ?"
            params = (True,)
        query += " ORDER BY id"
        async with self.db_manager.connection_scope() as connection:
            rows = await self._fetch(connection, query, params)
            return [row_to_gitops_provider(row) for row in rows]

    async def add_provider_allowlist(self, provider_id: str, repository_pattern: str) -> None:
        if not repository_pattern.strip():
            raise ValueError("repository pattern must not be blank")
        async with self._transaction() as connection:
            await self._execute(
                connection,
                """
                INSERT INTO gitops_provider_allowlists (provider_id, repository_pattern)
                VALUES (?, ?)
                ON CONFLICT (provider_id, repository_pattern) DO NOTHING
                """,
                (provider_id, repository_pattern),
            )

    async def list_provider_allowlists(self, provider_id: str) -> list[str]:
        async with self.db_manager.connection_scope() as connection:
            rows = await self._fetch(
                connection,
                """
                SELECT repository_pattern FROM gitops_provider_allowlists
                WHERE provider_id = ? ORDER BY repository_pattern
                """,
                (provider_id,),
            )
            return [str(row["repository_pattern"]) for row in rows]

    async def remove_provider_allowlist(self, provider_id: str, repository_pattern: str) -> bool:
        async with self._transaction() as connection:
            deleted = await self._execute(
                connection,
                """
                DELETE FROM gitops_provider_allowlists
                WHERE provider_id = ? AND repository_pattern = ?
                """,
                (provider_id, repository_pattern),
            )
            return deleted > 0

    async def _save_workload_source(self, connection: Any, source: WorkloadSource) -> None:
        safe_repo_url = sanitize_repository_url(source.repo_url)
        await self._execute(
            connection,
            """
            INSERT INTO workload_sources
                (id, provider_id, repo_url, ref, revision, path, controller_kind, controller_name,
                 controller_namespace, controller_uid, controller_generation, source_uid, source_generation,
                 source_revision, renderer, renderer_config, observed_at, expires_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                provider_id = excluded.provider_id,
                repo_url = excluded.repo_url,
                ref = excluded.ref,
                revision = excluded.revision,
                path = excluded.path,
                controller_kind = excluded.controller_kind,
                controller_name = excluded.controller_name,
                controller_namespace = excluded.controller_namespace,
                controller_uid = excluded.controller_uid,
                controller_generation = excluded.controller_generation,
                source_uid = excluded.source_uid,
                source_generation = excluded.source_generation,
                source_revision = excluded.source_revision,
                renderer = excluded.renderer,
                renderer_config = excluded.renderer_config,
                observed_at = excluded.observed_at,
                expires_at = excluded.expires_at,
                updated_at = excluded.updated_at
            """,
            (
                source.id,
                source.provider_id,
                safe_repo_url,
                source.ref,
                source.revision,
                source.path,
                source.controller_kind.value,
                source.controller_name,
                source.controller_namespace,
                source.controller_uid,
                source.controller_generation,
                source.source_uid,
                source.source_generation,
                source.source_revision,
                source.renderer.value,
                json_text(source.renderer_config),
                self._db_datetime(source.observed_at),
                self._db_datetime(source.expires_at),
                self._db_datetime(datetime.now(timezone.utc)),
            ),
            json_positions=(16,),
        )

    def _workload_binding_status_expression(self, binding_alias: str = "b") -> str:
        if self._postgres:
            expired_binding = f"({binding_alias}.expires_at IS NOT NULL AND {binding_alias}.expires_at <= ?)"
            old_binding = f"{binding_alias}.observed_at <= ?"
            expired_candidate = "(bc.expires_at IS NOT NULL AND bc.expires_at <= ?)"
            old_candidate = "bc.observed_at <= ?"
        else:
            expired_binding = (
                f"({binding_alias}.expires_at IS NOT NULL AND julianday({binding_alias}.expires_at) <= julianday(?))"
            )
            old_binding = f"julianday({binding_alias}.observed_at) <= julianday(?)"
            expired_candidate = "(bc.expires_at IS NOT NULL AND julianday(bc.expires_at) <= julianday(?))"
            old_candidate = "julianday(bc.observed_at) <= julianday(?)"
        return f"""
            CASE
                WHEN {binding_alias}.status IN ('resolved', 'ambiguous')
                 AND (
                    {expired_binding}
                    OR {old_binding}
                    OR EXISTS (
                        SELECT 1 FROM workload_binding_candidates bc
                        WHERE bc.binding_id = {binding_alias}.id
                          AND bc.source_id = {binding_alias}.source_id
                          AND (
                                bc.status = 'stale'
                                OR (
                                    bc.status IN ('resolved', 'ambiguous')
                                    AND ({expired_candidate} OR {old_candidate})
                                )
                          )
                    )
                 )
                THEN 'stale'
                ELSE {binding_alias}.status
            END
        """

    def _workload_binding_freshness_parameters(self, now: datetime) -> tuple[Any, Any, Any, Any]:
        current = _as_utc(now)
        ttl = configured_binding_ttl()
        return (
            self._db_datetime(current),
            self._db_datetime(current - ttl),
            self._db_datetime(current),
            self._db_datetime(current - ttl),
        )

    @staticmethod
    def _validate_workload_binding(binding: WorkloadBinding) -> None:
        if binding.status is BindingStatus.RESOLVED and binding.source is None:
            raise ValueError("resolved workload binding requires a selected source")
        if binding.status is BindingStatus.AMBIGUOUS and binding.source is not None:
            raise ValueError("ambiguous workload binding cannot select a source")

    async def _save_workload_binding_candidates_on_connection(
        self,
        connection: Any,
        binding_id: int,
        candidates: Sequence[WorkloadSource],
        *,
        now: datetime,
    ) -> None:
        candidate_ids = [candidate.id for candidate in candidates]
        if candidate_ids:
            await self._execute(
                connection,
                f"""
                DELETE FROM workload_binding_candidates
                WHERE binding_id = ?
                  AND source_id NOT IN ({", ".join("?" for _ in candidate_ids)})
                """,
                (binding_id, *candidate_ids),
            )
        else:
            await self._execute(
                connection,
                "DELETE FROM workload_binding_candidates WHERE binding_id = ?",
                (binding_id,),
            )
        for candidate in candidates:
            await self._execute(
                connection,
                """
                INSERT INTO workload_binding_candidates
                    (binding_id, source_id, status, confidence, evidence, reason,
                     observed_at, expires_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (binding_id, source_id) DO UPDATE SET
                    status = excluded.status,
                    confidence = excluded.confidence,
                    evidence = excluded.evidence,
                    reason = excluded.reason,
                    observed_at = excluded.observed_at,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (
                    binding_id,
                    candidate.id,
                    candidate.status.value,
                    candidate.confidence,
                    json_text(candidate.evidence),
                    candidate.reason,
                    self._db_datetime(candidate.observed_at),
                    self._db_datetime(candidate.expires_at),
                    self._db_datetime(now),
                ),
                json_positions=(5,),
            )

    async def _save_workload_binding_on_connection(self, connection: Any, binding: WorkloadBinding) -> None:
        self._validate_workload_binding(binding)
        now = datetime.now(timezone.utc)
        for source in binding.candidates:
            await self._save_workload_source(connection, source)
        await self._execute(
            connection,
            """
            INSERT INTO workload_source_bindings
                (cluster, namespace, workload_kind, workload_name, source_id, status, confidence,
                 evidence, reason, observed_at, expires_at, updated_at, generation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT (cluster, namespace, workload_kind, workload_name) DO UPDATE SET
                source_id = excluded.source_id,
                status = excluded.status,
                confidence = excluded.confidence,
                evidence = excluded.evidence,
                reason = excluded.reason,
                observed_at = excluded.observed_at,
                expires_at = excluded.expires_at,
                updated_at = excluded.updated_at,
                generation = workload_source_bindings.generation + 1
            """,
            (
                binding.cluster,
                binding.namespace,
                binding.workload_kind,
                binding.workload_name,
                binding.source.id if binding.source is not None else None,
                binding.status.value,
                binding.confidence,
                json_text(binding.evidence),
                binding.reason,
                self._db_datetime(binding.observed_at),
                self._db_datetime(binding.expires_at),
                self._db_datetime(now),
            ),
            json_positions=(8,),
        )
        binding_row = await self._fetchrow(
            connection,
            """
            SELECT id FROM workload_source_bindings
            WHERE cluster = ? AND namespace = ? AND workload_kind = ? AND workload_name = ?
            """,
            (binding.cluster, binding.namespace, binding.workload_kind, binding.workload_name),
        )
        assert binding_row is not None
        binding_id = int(binding_row["id"])
        await self._save_workload_binding_candidates_on_connection(
            connection,
            binding_id,
            binding.candidates,
            now=now,
        )

    async def _insert_workload_binding_event_on_connection(
        self,
        connection: Any,
        event: WorkloadBindingEvent,
    ) -> WorkloadBindingEvent:
        event_type = (
            event.event_type.value if isinstance(event.event_type, WorkloadBindingEventType) else event.event_type
        )
        values = (
            event.event_key,
            event.cluster,
            event.namespace,
            event.workload_kind,
            event.workload_name,
            event_type,
            event.actor,
            event.source_id,
            event.source_generation,
            self._db_datetime(event.expires_at),
            json_text(event.details),
            self._db_datetime(event.created_at),
        )
        row = await self._fetchrow(
            connection,
            """
            INSERT INTO workload_binding_events
                (event_key, cluster, namespace, workload_kind, workload_name, event_type, actor,
                 source_id, source_generation, expires_at, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (event_key) DO NOTHING
            RETURNING *
            """,
            values,
            json_positions=(11,),
        )
        if row is None:
            row = await self._fetchrow(
                connection,
                "SELECT * FROM workload_binding_events WHERE event_key = ?",
                (event.event_key,),
            )
        assert row is not None
        return row_to_workload_binding_event(row)

    async def save_workload_binding(self, binding: WorkloadBinding) -> WorkloadBinding:
        async with self._serialized_transaction() as connection:
            await self._save_workload_binding_on_connection(connection, binding)
        saved = await self.get_workload_binding(
            binding.cluster, binding.namespace, binding.workload_kind, binding.workload_name
        )
        assert saved is not None
        return saved

    async def save_workload_binding_with_event(
        self,
        binding: WorkloadBinding,
        event: WorkloadBindingEvent,
    ) -> WorkloadBinding:
        if _binding_event_identity(binding) != _binding_event_identity(event):
            raise ValueError("binding audit event must identify the saved workload")
        async with self._serialized_transaction() as connection:
            await self._save_workload_binding_on_connection(connection, binding)
            await self._insert_workload_binding_event_on_connection(connection, event)
            saved = await self._get_workload_binding_on_connection(
                connection, binding.cluster, binding.namespace, binding.workload_kind, binding.workload_name
            )
        assert saved is not None
        return saved

    async def save_discovered_workload_binding(
        self,
        binding: WorkloadBinding,
        *,
        expected_generation: int | None,
        events: Sequence[WorkloadBindingEvent] = (),
    ) -> WorkloadBinding | None:
        """Save discovery output only if no concurrent writer changed the binding."""
        self._validate_workload_binding(binding)
        if expected_generation is not None and (
            isinstance(expected_generation, bool) or not isinstance(expected_generation, int) or expected_generation < 1
        ):
            raise ValueError("expected workload binding generation must be a positive integer")
        if any(_binding_event_identity(binding) != _binding_event_identity(event) for event in events):
            raise ValueError("binding audit events must identify the saved workload")

        now = datetime.now(timezone.utc)
        try:
            async with self._serialized_transaction() as connection:
                if expected_generation is None:
                    row = await self._fetchrow(
                        connection,
                        """
                        INSERT INTO workload_source_bindings
                            (cluster, namespace, workload_kind, workload_name, source_id, status,
                             confidence, evidence, reason, observed_at, expires_at, generation)
                        VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 1)
                        ON CONFLICT (cluster, namespace, workload_kind, workload_name) DO NOTHING
                        RETURNING id
                        """,
                        (
                            binding.cluster,
                            binding.namespace,
                            binding.workload_kind,
                            binding.workload_name,
                            binding.status.value,
                            binding.confidence,
                            json_text(binding.evidence),
                            binding.reason,
                            self._db_datetime(binding.observed_at),
                            self._db_datetime(binding.expires_at),
                        ),
                        json_positions=(7,),
                    )
                    if row is None:
                        return None
                    binding_id = int(row["id"])
                    for source in binding.candidates:
                        await self._save_workload_source(connection, source)
                    if binding.source is not None:
                        await self._execute(
                            connection,
                            "UPDATE workload_source_bindings SET source_id = ? WHERE id = ?",
                            (binding.source.id, binding_id),
                        )
                else:
                    lock_clause = " FOR UPDATE" if self._postgres else ""
                    current = await self._fetchrow(
                        connection,
                        f"""
                        SELECT id, generation FROM workload_source_bindings
                        WHERE cluster = ? AND namespace = ? AND workload_kind = ? AND workload_name = ?
                        {lock_clause}
                        """,
                        (binding.cluster, binding.namespace, binding.workload_kind, binding.workload_name),
                    )
                    if current is None or int(current["generation"]) != expected_generation:
                        return None
                    previous_binding = await self._get_workload_binding_on_connection(
                        connection,
                        binding.cluster,
                        binding.namespace,
                        binding.workload_kind,
                        binding.workload_name,
                    )
                    if previous_binding is None:
                        return None
                    next_generation = expected_generation
                    if not _same_binding_material_state(previous_binding, binding):
                        next_generation += 1
                    for source in binding.candidates:
                        await self._save_workload_source(connection, source)
                    row = await self._fetchrow(
                        connection,
                        """
                        UPDATE workload_source_bindings
                        SET source_id = ?, status = ?, confidence = ?, evidence = ?, reason = ?,
                            observed_at = ?, expires_at = ?, updated_at = ?, generation = ?
                        WHERE cluster = ? AND namespace = ? AND workload_kind = ? AND workload_name = ?
                          AND generation = ?
                        RETURNING id
                        """,
                        (
                            binding.source.id if binding.source is not None else None,
                            binding.status.value,
                            binding.confidence,
                            json_text(binding.evidence),
                            binding.reason,
                            self._db_datetime(binding.observed_at),
                            self._db_datetime(binding.expires_at),
                            self._db_datetime(now),
                            next_generation,
                            binding.cluster,
                            binding.namespace,
                            binding.workload_kind,
                            binding.workload_name,
                            expected_generation,
                        ),
                        json_positions=(4,),
                    )
                    if row is None:
                        raise _BindingGenerationConflict
                    binding_id = int(row["id"])

                await self._save_workload_binding_candidates_on_connection(
                    connection,
                    binding_id,
                    binding.candidates,
                    now=now,
                )
                for event in events:
                    await self._insert_workload_binding_event_on_connection(connection, event)
                saved = await self._get_workload_binding_on_connection(
                    connection,
                    binding.cluster,
                    binding.namespace,
                    binding.workload_kind,
                    binding.workload_name,
                )
            return saved
        except _BindingGenerationConflict:
            return None

    async def save_manual_workload_binding(
        self,
        binding: WorkloadBinding,
        event: WorkloadBindingEvent,
        *,
        expected_candidate_id: str,
        expected_generation_token: str,
        now: datetime,
    ) -> WorkloadBinding:
        if binding.source is None or binding.status is not BindingStatus.RESOLVED:
            raise ValueError("manual binding resolution must select one resolved source")
        if event.event_type is not WorkloadBindingEventType.MANUAL_RESOLUTION_CREATED:
            raise ValueError("manual binding resolution requires a creation audit event")
        if _binding_event_identity(binding) != _binding_event_identity(event):
            raise ValueError("binding audit event must identify the saved workload")
        snapshot = binding.source.evidence.get("manual_resolution")
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("selected_candidate_id") != expected_candidate_id
            or snapshot.get("generation_token") != expected_generation_token
            or event.source_id != binding.source.id
            or event.source_generation != expected_generation_token
            or event.details.get("selected_candidate_id") != expected_candidate_id
            or event.expires_at != binding.source.expires_at
        ):
            raise ValueError("manual binding source generation does not match its audit event")

        observed_now = _as_utc(now)
        if (
            binding.source.expires_at is None
            or _as_utc(binding.source.expires_at) <= observed_now
            or _as_utc(binding.source.expires_at) > observed_now + configured_binding_ttl()
        ):
            raise ValueError("manual binding expiry must be bounded by the binding TTL")
        async with self._serialized_transaction() as connection:
            current = await self._get_workload_binding_on_connection(
                connection,
                binding.cluster,
                binding.namespace,
                binding.workload_kind,
                binding.workload_name,
                for_update=True,
            )
            if current is None or current.status not in {BindingStatus.AMBIGUOUS, BindingStatus.UNRESOLVED}:
                raise WorkloadBindingConflictError("workload binding is no longer actionable")
            candidate = next(
                (source for source in current.candidates if source.id == expected_candidate_id),
                None,
            )
            if (
                candidate is None
                or candidate.controller_kind.value == "manual_resolution"
                or binding.source.id != candidate.id
                or binding.source.controller_kind is not candidate.controller_kind
                or binding.source.controller_name != candidate.controller_name
                or binding.source.controller_namespace != candidate.controller_namespace
                or binding.source.controller_uid != candidate.controller_uid
                or binding.source.source_uid != candidate.source_uid
                or binding.source.source_generation != candidate.source_generation
                or binding.source.source_revision != candidate.source_revision
                or binding.source.repo_url != candidate.repo_url
                or binding.source.ref != candidate.ref
                or binding.source.revision != candidate.revision
                or binding.source.path != candidate.path
                or binding.source.renderer is not candidate.renderer
                or binding.source.renderer_config != candidate.renderer_config
                or candidate.status not in {BindingStatus.RESOLVED, BindingStatus.AMBIGUOUS}
                or candidate.provider_id == "unresolved"
                or candidate.repo_url == "unresolved"
                or binding_candidate_generation_token(candidate) != expected_generation_token
                or (candidate.expires_at is not None and _as_utc(candidate.expires_at) <= observed_now)
                or observed_now - _as_utc(candidate.observed_at) >= configured_binding_ttl()
            ):
                raise WorkloadBindingConflictError("workload binding candidate generation changed")
            await self._save_workload_binding_on_connection(connection, binding)
            await self._insert_workload_binding_event_on_connection(connection, event)
            saved = await self._get_workload_binding_on_connection(
                connection, binding.cluster, binding.namespace, binding.workload_kind, binding.workload_name
            )
        assert saved is not None
        return saved

    async def record_workload_binding_event(self, event: WorkloadBindingEvent) -> WorkloadBindingEvent:
        async with self._serialized_transaction() as connection:
            return await self._insert_workload_binding_event_on_connection(connection, event)

    async def list_workload_binding_events(
        self,
        *,
        cluster: str,
        namespace: str,
        workload_kind: str,
        workload_name: str,
        limit: int = 100,
    ) -> list[WorkloadBindingEvent]:
        if not 1 <= limit <= 500:
            raise ValueError("workload binding event limit must be between 1 and 500")
        async with self._serialized_connection() as connection:
            rows = await self._fetch(
                connection,
                """
                SELECT * FROM workload_binding_events
                WHERE cluster = ? AND namespace = ? AND workload_kind = ? AND workload_name = ?
                ORDER BY created_at ASC, id ASC LIMIT ?
                """,
                (cluster, namespace, workload_kind, workload_name, limit),
            )
        return [row_to_workload_binding_event(row) for row in rows]

    async def _get_workload_binding_on_connection(
        self,
        connection: Any,
        cluster: str,
        namespace: str,
        workload_kind: str,
        workload_name: str,
        *,
        for_update: bool = False,
    ) -> WorkloadBinding | None:
        now = datetime.now(timezone.utc)
        identity = (cluster, namespace, workload_kind, workload_name)
        lock_clause = " FOR UPDATE" if self._postgres and for_update else ""
        binding_row = await self._fetchrow(
            connection,
            f"""
            SELECT b.*, {self._workload_binding_status_expression("b")} AS effective_status
            FROM workload_source_bindings b
            WHERE b.cluster = ? AND b.namespace = ? AND b.workload_kind = ? AND b.workload_name = ?
            {lock_clause}
            """,
            (*self._workload_binding_freshness_parameters(now), *identity),
        )
        if binding_row is None:
            return None
        bindings = await self._load_workload_binding_projections_on_connection(
            connection,
            [binding_row],
            candidate_limit=MAX_WORKLOAD_BINDING_CANDIDATES,
            now=now,
        )
        return bindings[0] if bindings else None

    async def _load_workload_binding_projections_on_connection(
        self,
        connection: Any,
        binding_rows: Sequence[Any],
        *,
        candidate_limit: int,
        now: datetime,
    ) -> list[WorkloadBinding]:
        if not binding_rows:
            return []
        binding_ids = [int(row["id"]) for row in binding_rows]
        placeholders = ", ".join("?" for _ in binding_ids)
        candidate_rows = await self._fetch(
            connection,
            f"""
            WITH ranked_candidates AS (
                SELECT
                    b.cluster,
                    b.namespace AS workload_namespace,
                    b.workload_kind,
                    b.workload_name,
                    bc.binding_id,
                    bc.source_id AS candidate_source_id,
                    bc.status AS candidate_status,
                    bc.confidence AS candidate_confidence,
                    bc.evidence AS candidate_evidence,
                    bc.reason AS candidate_reason,
                    bc.observed_at AS candidate_observed_at,
                    bc.expires_at AS candidate_expires_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY bc.binding_id
                        ORDER BY CASE WHEN bc.source_id = b.source_id THEN 0 ELSE 1 END,
                                 bc.confidence DESC, bc.source_id
                    ) AS candidate_rank,
                    COUNT(*) OVER (PARTITION BY bc.binding_id) AS candidate_count,
                    ws.*
                FROM workload_binding_candidates bc
                JOIN workload_source_bindings b ON b.id = bc.binding_id
                JOIN workload_sources ws ON ws.id = bc.source_id
                WHERE bc.binding_id IN ({placeholders})
            )
            SELECT * FROM ranked_candidates
            WHERE candidate_rank <= ?
            ORDER BY binding_id, candidate_rank
            """,
            (*binding_ids, candidate_limit),
        )
        candidates_by_binding: dict[int, list[WorkloadSource]] = {binding_id: [] for binding_id in binding_ids}
        source_by_binding: dict[int, dict[str, WorkloadSource]] = {binding_id: {} for binding_id in binding_ids}
        candidate_counts: dict[int, int] = {}
        for candidate_row in candidate_rows:
            binding_id = int(candidate_row["binding_id"])
            source = row_to_workload_source(
                candidate_row,
                cluster=str(candidate_row["cluster"]),
                namespace=str(candidate_row["workload_namespace"]),
                workload_kind=str(candidate_row["workload_kind"]),
                workload_name=str(candidate_row["workload_name"]),
                binding_row={
                    "status": candidate_row["candidate_status"],
                    "confidence": candidate_row["candidate_confidence"],
                    "evidence": candidate_row["candidate_evidence"],
                    "reason": candidate_row["candidate_reason"],
                    "observed_at": candidate_row["candidate_observed_at"],
                    "expires_at": candidate_row["candidate_expires_at"],
                },
            )
            source = freshen_workload_source(source, now=now)
            candidates_by_binding[binding_id].append(source)
            source_by_binding[binding_id][source.id] = source
            candidate_counts[binding_id] = int(candidate_row["candidate_count"])

        bindings: list[WorkloadBinding] = []
        missing_selected_ids = {
            str(row["source_id"])
            for row in binding_rows
            if row["source_id"] is not None and str(row["source_id"]) not in source_by_binding[int(row["id"])]
        }
        selected_source_rows: dict[str, Any] = {}
        if missing_selected_ids:
            selected_placeholders = ", ".join("?" for _ in missing_selected_ids)
            selected_rows = await self._fetch(
                connection,
                f"SELECT * FROM workload_sources WHERE id IN ({selected_placeholders})",
                tuple(sorted(missing_selected_ids)),
            )
            selected_source_rows = {str(row["id"]): row for row in selected_rows}

        for binding_row in binding_rows:
            binding_id = int(binding_row["id"])
            cluster = str(binding_row["cluster"])
            namespace = str(binding_row["namespace"])
            workload_kind = str(binding_row["workload_kind"])
            workload_name = str(binding_row["workload_name"])
            source_id = str(binding_row["source_id"]) if binding_row["source_id"] is not None else None
            source = source_by_binding[binding_id].get(source_id) if source_id is not None else None
            if source is None and source_id is not None:
                source_row = selected_source_rows.get(source_id)
                if source_row is not None:
                    source = freshen_workload_source(
                        row_to_workload_source(
                            source_row,
                            cluster=cluster,
                            namespace=namespace,
                            workload_kind=workload_kind,
                            workload_name=workload_name,
                            binding_row=binding_row,
                        ),
                        now=now,
                    )
            binding = row_to_workload_binding(
                binding_row,
                source=source,
                candidates=tuple(candidates_by_binding[binding_id]),
                candidates_truncated=candidate_counts.get(binding_id, 0) > candidate_limit,
            )
            bindings.append(freshen_workload_binding(binding, now=now))
        return bindings

    async def get_workload_binding(
        self, cluster: str, namespace: str, workload_kind: str, workload_name: str
    ) -> WorkloadBinding | None:
        async with self._serialized_connection() as connection:
            return await self._get_workload_binding_on_connection(
                connection, cluster, namespace, workload_kind, workload_name
            )

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
        _validate_binding_page(limit=limit, candidate_limit=candidate_limit)
        selected_statuses = (
            None if statuses is None else tuple(dict.fromkeys(BindingStatus(status) for status in statuses))
        )
        if selected_statuses is not None and not selected_statuses:
            return WorkloadBindingPage((), None, False)
        current_at = _as_utc(now or datetime.now(timezone.utc))
        clauses: list[str] = []
        params: list[Any] = list(self._workload_binding_freshness_parameters(current_at))
        if cluster is not None:
            clauses.append("cluster = ?")
            params.append(cluster)
        if cursor is not None:
            cursor_identity = _binding_identity_from_cursor(cursor)
            if cluster is not None and cursor_identity[0] != cluster:
                raise ValueError("workload binding cursor does not match the selected cluster")
            clauses.append("(cluster, namespace, workload_kind, workload_name) > (?, ?, ?, ?)")
            params.extend(cursor_identity)
        if selected_statuses is not None:
            clauses.append(f"effective_status IN ({', '.join('?' for _ in selected_statuses)})")
            params.extend(status.value for status in selected_statuses)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit + 1)
        query = f"""
            WITH projected AS (
                SELECT b.*, {self._workload_binding_status_expression("b")} AS effective_status
                FROM workload_source_bindings b
            )
            SELECT * FROM projected
            {where}
            ORDER BY cluster, namespace, workload_kind, workload_name
            LIMIT ?
        """
        async with self._serialized_connection() as connection:
            binding_rows = await self._fetch(connection, query, params)
            has_more = len(binding_rows) > limit
            binding_rows = binding_rows[:limit]
            bindings = await self._load_workload_binding_projections_on_connection(
                connection,
                binding_rows,
                candidate_limit=candidate_limit,
                now=current_at,
            )
        next_cursor = None
        if has_more and bindings:
            last = bindings[-1]
            next_cursor = _binding_identity_cursor(
                (last.cluster, last.namespace, last.workload_kind, last.workload_name)
            )
        return WorkloadBindingPage(tuple(bindings), next_cursor, has_more)

    async def list_workload_bindings(self, *, cluster: str | None = None) -> list[WorkloadBinding]:
        bindings: list[WorkloadBinding] = []
        cursor: str | None = None
        while True:
            page = await self.list_workload_bindings_page(cluster=cluster, cursor=cursor)
            bindings.extend(page.items)
            if not page.has_more:
                return bindings
            cursor = page.next_cursor
            assert cursor is not None

    async def get_workload_binding_coverage(
        self,
        *,
        cluster: str | None = None,
        now: datetime | None = None,
    ) -> WorkloadBindingCoverage:
        current_at = _as_utc(now or datetime.now(timezone.utc))
        status_expression = self._workload_binding_status_expression("b")
        clauses: list[str] = []
        params: list[Any] = list(self._workload_binding_freshness_parameters(current_at))
        if cluster is not None:
            clauses.append("cluster = ?")
            params.append(cluster)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"""
            WITH projected AS (
                SELECT b.*, {status_expression} AS effective_status
                FROM workload_source_bindings b
            )
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN effective_status = 'resolved' THEN 1 ELSE 0 END) AS resolved,
                SUM(CASE WHEN effective_status = 'ambiguous' THEN 1 ELSE 0 END) AS ambiguous,
                SUM(CASE WHEN effective_status = 'unresolved' THEN 1 ELSE 0 END) AS unresolved,
                SUM(CASE WHEN effective_status = 'blocked' THEN 1 ELSE 0 END) AS blocked,
                SUM(CASE WHEN effective_status = 'stale' THEN 1 ELSE 0 END) AS stale,
                MAX(observed_at) AS last_refresh
            FROM projected
            {where}
        """
        async with self._serialized_connection() as connection:
            row = await self._fetchrow(connection, query, params)
        assert row is not None
        return WorkloadBindingCoverage(
            total=int(row["total"] or 0),
            resolved=int(row["resolved"] or 0),
            ambiguous=int(row["ambiguous"] or 0),
            unresolved=int(row["unresolved"] or 0),
            blocked=int(row["blocked"] or 0),
            stale=int(row["stale"] or 0),
            last_refresh=datetime_value(row["last_refresh"]),
        )

    async def save_plan(
        self,
        plan: Plan,
        *,
        policy_result: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> Plan:
        return await self._save_plan(plan, policy_result=policy_result, insert_only=False, actor=actor)

    async def save_plan_if_absent(
        self,
        plan: Plan,
        *,
        policy_result: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> Plan:
        return await self._save_plan(plan, policy_result=policy_result, insert_only=True, actor=actor)

    async def set_plan_policy_result(
        self,
        plan_id: str,
        policy_result: dict[str, Any],
        *,
        expected_digest: str,
        expected_statuses: Sequence[PlanStatus] | None = None,
        actor: str = "system",
    ) -> Plan | None:
        actor = _validated_audit_actor(actor)
        async with self._transaction() as connection:
            row_lock = " FOR UPDATE" if self._postgres else ""
            row = await self._fetchrow(
                connection,
                f"SELECT policy_result, digest, status FROM automation_plans WHERE plan_id = ?{row_lock}",
                (plan_id,),
            )
            if row is None or row["digest"] != expected_digest:
                return None
            if expected_statuses is not None and PlanStatus(row["status"]) not in expected_statuses:
                return None

            existing_policy = json_value(row["policy_result"], {})
            if not isinstance(existing_policy, dict):
                existing_policy = {}
            updated_policy = dict(policy_result)
            baseline = existing_policy.get(PLAN_PRE_APPLY_BASELINE_POLICY_KEY)
            if baseline is not None:
                updated_policy[PLAN_PRE_APPLY_BASELINE_POLICY_KEY] = baseline

            query = f"""
                UPDATE automation_plans
                SET policy_result = ?, updated_at = {self._now_expression}
                WHERE plan_id = ? AND digest = ?
            """
            params: list[Any] = [json_text(updated_policy), plan_id, expected_digest]
            if expected_statuses is not None:
                if not expected_statuses:
                    return None
                placeholders = ", ".join("?" for _ in expected_statuses)
                query += f" AND status IN ({placeholders})"
                params.extend(status.value for status in expected_statuses)
            updated = await self._execute(
                connection,
                query,
                params,
                json_positions=(1,),
            )
            if not updated:
                return None

            recommendation_rows = await self._fetch(
                connection,
                """
                SELECT recommendation_id FROM automation_plan_recommendations
                WHERE plan_id = ? ORDER BY recommendation_id
                """,
                (plan_id,),
            )
            details = {
                "plan_id": plan_id,
                "digest": expected_digest,
                "allowed": policy_result.get("allowed") is True,
                "policy_result": policy_result,
            }
            for recommendation_row in recommendation_rows:
                await self._record_recommendation_audit_event(
                    connection,
                    int(recommendation_row["recommendation_id"]),
                    event_type="plan_policy_evaluated",
                    actor=actor,
                    details=details,
                )

            record = await self._get_plan_record_on_connection(connection, plan_id)
            return record.plan if record is not None else None

    async def _save_plan(
        self,
        plan: Plan,
        *,
        policy_result: dict[str, Any] | None,
        insert_only: bool,
        actor: str,
    ) -> Plan:
        actor = _validated_audit_actor(actor)
        file_plan = plan.file_plan
        if file_plan is not None and plan.digest is not None and plan.digest != file_plan.digest:
            raise ValueError("plan digest must match its file plan digest")
        digest = plan.digest if plan.digest is not None else (file_plan.digest if file_plan is not None else None)
        validation = file_plan.validation.model_dump(mode="json") if file_plan is not None else {}
        warnings = file_plan.warnings if file_plan is not None else []
        safe_source = plan.source.model_copy(update={"repo_url": sanitize_repository_url(plan.source.repo_url)})
        source_snapshot = safe_source.model_dump(mode="json")
        action_json = plan.action.model_dump(mode="json")
        now = datetime.now(timezone.utc)
        conflict_clause = (
            "ON CONFLICT (plan_id) DO NOTHING"
            if insert_only
            else """
            ON CONFLICT (plan_id) DO UPDATE SET
                source_id = excluded.source_id,
                provider_id = excluded.provider_id,
                source_snapshot = excluded.source_snapshot,
                action_json = excluded.action_json,
                change_set_id = excluded.change_set_id,
                rollback_of_plan_id = excluded.rollback_of_plan_id,
                base_revision = excluded.base_revision,
                source_revision = excluded.source_revision,
                renderer = excluded.renderer,
                diff = excluded.diff,
                rendered_diff = excluded.rendered_diff,
                digest = excluded.digest,
                validation = excluded.validation,
                warnings = excluded.warnings,
                policy_result = excluded.policy_result,
                status = excluded.status,
                expires_at = excluded.expires_at,
                updated_at = excluded.updated_at
            """
        )
        async with self._transaction() as connection:
            row_lock = " FOR UPDATE" if self._postgres else ""
            existing_row = await self._fetchrow(
                connection,
                f"SELECT policy_result FROM automation_plans WHERE plan_id = ?{row_lock}",
                (plan.plan_id,),
            )
            policy_payload = dict(policy_result) if policy_result is not None else dict(plan.policy_result)
            if existing_row is not None:
                existing_policy = json_value(existing_row["policy_result"], {})
                if not isinstance(existing_policy, dict):
                    existing_policy = {}
                existing_baseline_data = existing_policy.get(PLAN_PRE_APPLY_BASELINE_POLICY_KEY)
                if existing_baseline_data is not None:
                    existing_baseline = PreApplyWorkloadBaseline.model_validate(existing_baseline_data)
                    if (
                        not insert_only
                        and plan.pre_apply_baseline is not None
                        and plan.pre_apply_baseline != existing_baseline
                    ):
                        raise ValueError("a persisted pre-apply workload baseline is immutable")
                    policy_payload[PLAN_PRE_APPLY_BASELINE_POLICY_KEY] = existing_baseline.model_dump(mode="json")
                elif plan.pre_apply_baseline is not None:
                    policy_payload[PLAN_PRE_APPLY_BASELINE_POLICY_KEY] = plan.pre_apply_baseline.model_dump(mode="json")
            elif plan.pre_apply_baseline is not None:
                policy_payload[PLAN_PRE_APPLY_BASELINE_POLICY_KEY] = plan.pre_apply_baseline.model_dump(mode="json")
            await self._save_workload_source(connection, plan.source)
            inserted = await self._execute(
                connection,
                f"""
                INSERT INTO automation_plans
                    (plan_id, source_id, provider_id, source_snapshot, action_json, change_set_id,
                     rollback_of_plan_id, base_revision, source_revision, renderer, diff, rendered_diff,
                     digest, validation, warnings, policy_result, status, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                {conflict_clause}
                """,
                (
                    plan.plan_id,
                    plan.source.id,
                    plan.source.provider_id,
                    json_text(source_snapshot),
                    json_text(action_json),
                    plan.change_set_id,
                    plan.rollback_of_plan_id,
                    file_plan.base_revision if file_plan is not None else None,
                    file_plan.source_revision if file_plan is not None else None,
                    file_plan.renderer.value if file_plan is not None else None,
                    file_plan.diff if file_plan is not None else None,
                    file_plan.rendered_diff if file_plan is not None else None,
                    digest,
                    json_text(validation),
                    json_text(warnings),
                    json_text(policy_payload),
                    plan.status.value,
                    self._db_datetime(plan.expires_at),
                    self._db_datetime(plan.created_at),
                    self._db_datetime(now),
                ),
                json_positions=(4, 5, 14, 15, 16),
            )
            if not insert_only or inserted:
                await self._execute(
                    connection,
                    "DELETE FROM automation_plan_files WHERE plan_id = ?",
                    (plan.plan_id,),
                )
                if file_plan is not None:
                    for edit in file_plan.files:
                        await self._execute(
                            connection,
                            """
                            INSERT INTO automation_plan_files (plan_id, path, operation, original_sha, content)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (plan.plan_id, edit.path, edit.operation, edit.original_sha, edit.content),
                        )
                await self._execute(
                    connection,
                    "DELETE FROM automation_plan_recommendations WHERE plan_id = ?",
                    (plan.plan_id,),
                )
                for recommendation_id in plan.recommendation_ids:
                    await self._execute(
                        connection,
                        """
                        INSERT INTO automation_plan_recommendations (plan_id, recommendation_id)
                        VALUES (?, ?)
                        ON CONFLICT (plan_id, recommendation_id) DO NOTHING
                        """,
                        (plan.plan_id, recommendation_id),
                    )
                if existing_row is None and inserted:
                    details = {
                        "plan_id": plan.plan_id,
                        "digest": digest,
                        "provider_id": plan.source.provider_id,
                        "source_id": plan.source.id,
                        "renderer": plan.source.renderer.value,
                        "base_revision": file_plan.base_revision if file_plan is not None else None,
                        "policy_result": policy_payload,
                    }
                    for recommendation_id in plan.recommendation_ids:
                        await self._record_recommendation_audit_event(
                            connection,
                            recommendation_id,
                            event_type="plan_created",
                            actor=actor,
                            details=details,
                        )
        saved = await self.get_plan(plan.plan_id)
        assert saved is not None
        return saved

    async def set_plan_pre_apply_baseline_if_absent(
        self,
        plan_id: str,
        baseline: PreApplyWorkloadBaseline,
    ) -> Plan | None:
        async with self._transaction() as connection:
            for _ in range(5):
                row = await self._fetchrow(
                    connection,
                    "SELECT policy_result FROM automation_plans WHERE plan_id = ?",
                    (plan_id,),
                )
                if row is None:
                    return None
                raw_policy = row["policy_result"]
                policy_result = json_value(raw_policy, {})
                if not isinstance(policy_result, dict):
                    policy_result = {}
                else:
                    policy_result = dict(policy_result)
                existing_baseline = policy_result.get(PLAN_PRE_APPLY_BASELINE_POLICY_KEY)
                if existing_baseline is not None:
                    record = await self._get_plan_record_on_connection(connection, plan_id)
                    return record.plan if record is not None else None

                updated_policy = dict(policy_result)
                updated_policy[PLAN_PRE_APPLY_BASELINE_POLICY_KEY] = baseline.model_dump(mode="json")
                if raw_policy is None:
                    updated = await self._execute(
                        connection,
                        f"""
                        UPDATE automation_plans
                        SET policy_result = ?, updated_at = {self._now_expression}
                        WHERE plan_id = ? AND policy_result IS NULL
                        """,
                        (json_text(updated_policy), plan_id),
                        json_positions=(1,),
                    )
                else:
                    expected_policy = raw_policy if isinstance(raw_policy, str) else json_text(policy_result)
                    updated = await self._execute(
                        connection,
                        f"""
                        UPDATE automation_plans
                        SET policy_result = ?, updated_at = {self._now_expression}
                        WHERE plan_id = ? AND policy_result = ?
                        """,
                        (json_text(updated_policy), plan_id, expected_policy),
                        json_positions=(1, 3),
                    )
                if updated:
                    record = await self._get_plan_record_on_connection(connection, plan_id)
                    return record.plan if record is not None else None
        raise RuntimeError("could not persist the immutable pre-apply workload baseline after concurrent updates")

    async def _get_plan_record_on_connection(self, connection: Any, plan_id: str) -> AutomationPlanRecord | None:
        row = await self._fetchrow(connection, "SELECT * FROM automation_plans WHERE plan_id = ?", (plan_id,))
        if row is None:
            return None
        file_rows = await self._fetch(
            connection,
            "SELECT * FROM automation_plan_files WHERE plan_id = ? ORDER BY id",
            (plan_id,),
        )
        recommendation_rows = await self._fetch(
            connection,
            """
            SELECT recommendation_id FROM automation_plan_recommendations
            WHERE plan_id = ? ORDER BY recommendation_id
            """,
            (plan_id,),
        )
        return row_to_automation_plan_record(
            row,
            file_rows=file_rows,
            recommendation_ids=[int(item["recommendation_id"]) for item in recommendation_rows],
        )

    async def get_plan_record(self, plan_id: str) -> AutomationPlanRecord | None:
        async with self.db_manager.connection_scope() as connection:
            return await self._get_plan_record_on_connection(connection, plan_id)

    async def get_plan(self, plan_id: str) -> Plan | None:
        record = await self.get_plan_record(plan_id)
        return record.plan if record is not None else None

    async def list_plans(self, *, status: PlanStatus | None = None, source_id: str | None = None) -> list[Plan]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)
        query = "SELECT plan_id FROM automation_plans"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, plan_id"
        async with self.db_manager.connection_scope() as connection:
            rows = await self._fetch(connection, query, params)
            plans = []
            for row in rows:
                record = await self._get_plan_record_on_connection(connection, str(row["plan_id"]))
                if record is not None:
                    plans.append(record.plan)
            return plans

    async def list_plans_for_workload(
        self,
        *,
        cluster: str,
        namespace: str,
        workload_kind: str,
        workload_name: str,
        statuses: Sequence[PlanStatus] | None = None,
    ) -> list[Plan]:
        if statuses is not None and not statuses:
            return []
        if self._postgres:
            identity_columns = (
                "source_snapshot->>'cluster'",
                "source_snapshot->>'namespace'",
                "source_snapshot->>'workload_kind'",
                "source_snapshot->>'workload_name'",
            )
        else:
            identity_columns = (
                "json_extract(source_snapshot, '$.cluster')",
                "json_extract(source_snapshot, '$.namespace')",
                "json_extract(source_snapshot, '$.workload_kind')",
                "json_extract(source_snapshot, '$.workload_name')",
            )
        clauses = [f"{column} = ?" for column in identity_columns]
        params: list[Any] = [cluster, namespace, workload_kind, workload_name]
        if statuses is not None:
            clauses.append(f"status IN ({', '.join('?' for _ in statuses)})")
            params.extend(status.value for status in statuses)
        query = "SELECT plan_id FROM automation_plans WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, plan_id"
        async with self.db_manager.connection_scope() as connection:
            rows = await self._fetch(connection, query, params)
            plans = []
            for row in rows:
                record = await self._get_plan_record_on_connection(connection, str(row["plan_id"]))
                if record is not None:
                    plans.append(record.plan)
            return plans

    async def prune_automation_history(self, *, cutoff: datetime) -> dict[str, int]:
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("automation history cutoff must be timezone-aware")
        terminal_statuses = (
            PlanStatus.APPLIED.value,
            PlanStatus.CLOSED.value,
            PlanStatus.FAILED.value,
            PlanStatus.REJECTED.value,
            PlanStatus.STALE.value,
            PlanStatus.SUPERSEDED.value,
            PlanStatus.SYNC_FAILED.value,
        )
        placeholders = ", ".join("?" for _ in terminal_statuses)
        updated_before = "updated_at < ?" if self._postgres else "julianday(updated_at) < julianday(?)"
        processed_before = "processed_at < ?" if self._postgres else "julianday(processed_at) < julianday(?)"
        cutoff_value = self._db_datetime(cutoff)
        async with self._transaction() as connection:
            plan_files = await self._execute(
                connection,
                f"""
                DELETE FROM automation_plan_files
                WHERE plan_id IN (
                    SELECT plan_id FROM automation_plans
                    WHERE status IN ({placeholders}) AND {updated_before}
                )
                """,
                (*terminal_statuses, cutoff_value),
            )
            operations = await self._execute(
                connection,
                f"""
                DELETE FROM automation_plan_operations
                WHERE status IN ('succeeded', 'failed', 'cancelled') AND {updated_before}
                """,
                (cutoff_value,),
            )
            webhooks = await self._execute(
                connection,
                f"""
                DELETE FROM webhook_deliveries
                WHERE status IN ('processed', 'rejected') AND processed_at IS NOT NULL
                  AND {processed_before}
                """,
                (cutoff_value,),
            )
            recommendation_events = await self._execute(
                connection,
                f"DELETE FROM recommendation_events WHERE {updated_before.replace('updated_at', 'created_at')}",
                (cutoff_value,),
            )
            binding_events = await self._execute(
                connection,
                f"DELETE FROM workload_binding_events WHERE {updated_before.replace('updated_at', 'created_at')}",
                (cutoff_value,),
            )
        return {
            "plan_files": plan_files,
            "operations": operations,
            "webhooks": webhooks,
            "recommendation_events": recommendation_events,
            "binding_events": binding_events,
        }

    async def set_plan_status(
        self,
        plan_id: str,
        status: PlanStatus,
        *,
        expected_statuses: Sequence[PlanStatus] | None = None,
        expected_digest: str | None = None,
        actor: str = "system",
        reason_code: str | None = None,
    ) -> Plan | None:
        actor = _validated_audit_actor(actor)
        conditions = ["plan_id = ?"]
        params: list[Any] = [status.value, plan_id]
        expected = tuple(dict.fromkeys(expected_statuses or ()))
        if expected_statuses is not None:
            if not expected:
                raise ValueError("expected_statuses must not be empty")
            placeholders = ", ".join("?" for _ in expected)
            conditions.append(f"status IN ({placeholders})")
            params.extend(item.value for item in expected)
        if expected_digest is not None:
            conditions.append("digest = ?")
            params.append(expected_digest)

        async with self._transaction() as connection:
            row_lock = " FOR UPDATE" if self._postgres else ""
            previous = await self._fetchrow(
                connection,
                f"""
                SELECT status, digest, source_id, provider_id
                FROM automation_plans
                WHERE plan_id = ?{row_lock}
                """,
                (plan_id,),
            )
            if previous is None:
                return None
            previous_status = str(previous["status"])
            if expected and previous_status not in {item.value for item in expected}:
                return None
            if expected_digest is not None and previous["digest"] != expected_digest:
                return None
            updated = await self._execute(
                connection,
                f"""
                UPDATE automation_plans
                SET status = ?, updated_at = {self._now_expression}
                WHERE {" AND ".join(conditions)}
                """,
                params,
            )
            if not updated:
                return None
            if previous_status != status.value:
                recommendation_rows = await self._fetch(
                    connection,
                    """
                    SELECT recommendation_id FROM automation_plan_recommendations
                    WHERE plan_id = ? ORDER BY recommendation_id
                    """,
                    (plan_id,),
                )
                details: dict[str, Any] = {
                    "plan_id": plan_id,
                    "digest": previous["digest"],
                    "provider_id": previous["provider_id"],
                    "source_id": previous["source_id"],
                    "previous_status": previous_status,
                    "new_status": status.value,
                }
                if reason_code is not None:
                    details["reason_code"] = reason_code
                for recommendation_row in recommendation_rows:
                    await self._record_recommendation_audit_event(
                        connection,
                        int(recommendation_row["recommendation_id"]),
                        event_type="plan_status_changed",
                        actor=actor,
                        details=details,
                    )
            record = await self._get_plan_record_on_connection(connection, plan_id)
            return record.plan if record is not None else None

    async def list_recommendation_ids_for_plan(self, plan_id: str) -> list[int]:
        async with self.db_manager.connection_scope() as connection:
            rows = await self._fetch(
                connection,
                """
                SELECT recommendation_id FROM automation_plan_recommendations
                WHERE plan_id = ? ORDER BY recommendation_id
                """,
                (plan_id,),
            )
            return [int(row["recommendation_id"]) for row in rows]

    async def list_plans_for_recommendation(self, recommendation_id: int) -> list[Plan]:
        async with self.db_manager.connection_scope() as connection:
            rows = await self._fetch(
                connection,
                """
                SELECT plan_id FROM automation_plan_recommendations
                WHERE recommendation_id = ? ORDER BY plan_id
                """,
                (recommendation_id,),
            )
            plans = []
            for row in rows:
                record = await self._get_plan_record_on_connection(connection, str(row["plan_id"]))
                if record is not None:
                    plans.append(record.plan)
            return plans

    async def set_recommendation_action_status(
        self,
        recommendation_ids: Sequence[int],
        status: ActionStatus,
        *,
        expected_statuses: Sequence[ActionStatus] | None = None,
        actor: str = "system",
        plan_id: str | None = None,
    ) -> int:
        actor = _validated_audit_actor(actor)
        ids = tuple(dict.fromkeys(recommendation_ids))
        if not ids:
            return 0
        if any(recommendation_id <= 0 for recommendation_id in ids):
            raise ValueError("recommendation ids must be positive integers")
        conditions = [f"id IN ({', '.join('?' for _ in ids)})"]
        params: list[Any] = [status.value, *ids]
        expected = tuple(dict.fromkeys(expected_statuses or ()))
        if expected_statuses is not None:
            if not expected:
                raise ValueError("expected_statuses must not be empty")
            placeholders = ", ".join("?" for _ in expected)
            conditions.append(f"action_status IN ({placeholders})")
            params.extend(item.value for item in expected)
        async with self._transaction() as connection:
            id_placeholders = ", ".join("?" for _ in ids)
            row_lock = " FOR UPDATE" if self._postgres else ""
            rows = await self._fetch(
                connection,
                f"""
                SELECT id, action_status FROM recommendation_history
                WHERE id IN ({id_placeholders}){row_lock}
                """,
                ids,
            )
            expected_values = {item.value for item in expected}
            changed_rows = [
                row
                for row in rows
                if row["action_status"] != status.value
                and (expected_statuses is None or row["action_status"] in expected_values)
            ]
            updated = await self._execute(
                connection,
                "UPDATE recommendation_history SET action_status = ? WHERE " + " AND ".join(conditions),
                params,
            )
            for row in changed_rows:
                recommendation_id = int(row["id"])
                if plan_id is not None:
                    plan_ids = [plan_id]
                else:
                    linked_plans = await self._fetch(
                        connection,
                        """
                        SELECT plan_id FROM automation_plan_recommendations
                        WHERE recommendation_id = ? ORDER BY plan_id
                        """,
                        (recommendation_id,),
                    )
                    plan_ids = [str(linked["plan_id"]) for linked in linked_plans]
                await self._record_recommendation_audit_event(
                    connection,
                    recommendation_id,
                    event_type="plan_action_status_changed",
                    actor=actor,
                    details={
                        "plan_ids": plan_ids,
                        "previous_action_status": row["action_status"],
                        "new_action_status": status.value,
                    },
                )
            return updated

    async def get_recommendation_action_status(self, recommendation_id: int) -> ActionStatus | None:
        async with self.db_manager.connection_scope() as connection:
            row = await self._fetchrow(
                connection,
                "SELECT action_status FROM recommendation_history WHERE id = ?",
                (recommendation_id,),
            )
            return ActionStatus(row["action_status"]) if row is not None else None

    async def list_recommendations_by_action_status(self, status: ActionStatus) -> list[int]:
        async with self.db_manager.connection_scope() as connection:
            rows = await self._fetch(
                connection,
                """
                SELECT id FROM recommendation_history
                WHERE action_status = ? ORDER BY id
                """,
                (status.value,),
            )
            return [int(row["id"]) for row in rows]

    async def record_webhook_delivery(
        self, event: PullRequestEvent, *, event_type: str = "pull_request"
    ) -> tuple[WebhookDelivery, bool]:
        if not event_type.strip():
            raise ValueError("webhook event type must not be blank")
        async with self._transaction() as connection:
            inserted = await self._execute(
                connection,
                """
                INSERT INTO webhook_deliveries
                    (provider_id, delivery_id, event_type, repo, pr_number, action, merged,
                     merge_commit_sha, head_sha, base_sha, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (provider_id, delivery_id) DO NOTHING
                """,
                (
                    event.provider_id,
                    event.delivery_id,
                    event_type,
                    event.repo,
                    event.number,
                    event.action,
                    event.merged,
                    event.merge_commit_sha,
                    event.head_sha,
                    event.base_sha,
                    self._db_datetime(event.occurred_at),
                ),
            )
            row = await self._fetchrow(
                connection,
                """
                SELECT * FROM webhook_deliveries
                WHERE provider_id = ? AND delivery_id = ?
                """,
                (event.provider_id, event.delivery_id),
            )
            assert row is not None
            return row_to_webhook_delivery(row), inserted > 0

    async def get_webhook_delivery(self, provider_id: str, delivery_id: str) -> WebhookDelivery | None:
        async with self.db_manager.connection_scope() as connection:
            row = await self._fetchrow(
                connection,
                """
                SELECT * FROM webhook_deliveries
                WHERE provider_id = ? AND delivery_id = ?
                """,
                (provider_id, delivery_id),
            )
            return row_to_webhook_delivery(row) if row is not None else None

    async def update_webhook_delivery(
        self,
        provider_id: str,
        delivery_id: str,
        *,
        status: str,
        processed_at: datetime | None = None,
        error_code: str | None = None,
        expected_statuses: Sequence[str] | None = None,
    ) -> WebhookDelivery | None:
        if not status.strip():
            raise ValueError("webhook delivery status must not be blank")
        conditions = ["provider_id = ?", "delivery_id = ?"]
        params: list[Any] = [
            status,
            self._db_datetime(processed_at),
            error_code,
            provider_id,
            delivery_id,
        ]
        expected = tuple(dict.fromkeys(expected_statuses or ()))
        if expected_statuses is not None:
            if not expected:
                raise ValueError("expected_statuses must not be empty")
            conditions.append(f"status IN ({', '.join('?' for _ in expected)})")
            params.extend(expected)
        async with self._transaction() as connection:
            updated = await self._execute(
                connection,
                f"""
                UPDATE webhook_deliveries
                SET status = ?, processed_at = ?, error_code = ?, updated_at = {self._now_expression}
                WHERE {" AND ".join(conditions)}
                """,
                params,
            )
        return await self.get_webhook_delivery(provider_id, delivery_id) if updated else None

    async def claim_webhook_delivery(
        self,
        provider_id: str,
        delivery_id: str,
        *,
        stale_before: datetime,
    ) -> bool:
        if stale_before.tzinfo is None or stale_before.utcoffset() is None:
            raise ValueError("webhook processing cutoff must be timezone-aware")
        stale_predicate = "updated_at < ?" if self._postgres else "julianday(updated_at) < julianday(?)"
        async with self._transaction() as connection:
            updated = await self._execute(
                connection,
                f"""
                UPDATE webhook_deliveries
                SET status = 'processing', processed_at = NULL, error_code = NULL,
                    updated_at = {self._now_expression}
                WHERE provider_id = ? AND delivery_id = ?
                  AND (
                    status IN ('received', 'retryable', 'reconcile_unavailable')
                    OR (status = 'processing' AND {stale_predicate})
                  )
                """,
                (provider_id, delivery_id, self._db_datetime(stale_before)),
            )
            return updated == 1

    async def list_pending_webhook_deliveries(
        self,
        *,
        limit: int,
        stale_before: datetime,
    ) -> list[WebhookDelivery]:
        if not 1 <= limit <= 100:
            raise ValueError("pending webhook limit must be between 1 and 100")
        if stale_before.tzinfo is None or stale_before.utcoffset() is None:
            raise ValueError("webhook processing cutoff must be timezone-aware")
        stale_predicate = "updated_at < ?" if self._postgres else "julianday(updated_at) < julianday(?)"
        async with self.db_manager.connection_scope() as connection:
            rows = await self._fetch(
                connection,
                f"""
                SELECT * FROM webhook_deliveries
                WHERE status = 'received'
                   OR (
                     status IN ('retryable', 'reconcile_unavailable', 'processing')
                     AND {stale_predicate}
                   )
                ORDER BY received_at, id LIMIT ?
                """,
                (self._db_datetime(stale_before), limit),
            )
            return [row_to_webhook_delivery(row) for row in rows]

    async def upsert_worker_heartbeat(self, heartbeat: WorkerHeartbeat) -> WorkerHeartbeat:
        async with self._transaction() as connection:
            await self._execute(
                connection,
                """
                INSERT INTO automation_worker_heartbeats
                    (worker_id, status, heartbeat_at, lease_id, lease_owner, lease_expires_at,
                     lease_operation_id, lease_plan_operation_id, metadata, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (worker_id) DO UPDATE SET
                    status = excluded.status,
                    heartbeat_at = excluded.heartbeat_at,
                    lease_id = excluded.lease_id,
                    lease_owner = excluded.lease_owner,
                    lease_expires_at = excluded.lease_expires_at,
                    lease_operation_id = excluded.lease_operation_id,
                    lease_plan_operation_id = excluded.lease_plan_operation_id,
                    metadata = excluded.metadata,
                    updated_at = excluded.updated_at
                """,
                (
                    heartbeat.worker_id,
                    heartbeat.status,
                    self._db_datetime(heartbeat.heartbeat_at),
                    heartbeat.lease_id,
                    heartbeat.lease_owner,
                    self._db_datetime(heartbeat.lease_expires_at),
                    heartbeat.lease_operation_id,
                    heartbeat.lease_plan_operation_id,
                    json_text(heartbeat.metadata),
                    self._db_datetime(datetime.now(timezone.utc)),
                ),
                json_positions=(9,),
            )
            row = await self._fetchrow(
                connection,
                "SELECT * FROM automation_worker_heartbeats WHERE worker_id = ?",
                (heartbeat.worker_id,),
            )
            assert row is not None
            return row_to_worker_heartbeat(row)

    async def get_worker_heartbeat(self, worker_id: str) -> WorkerHeartbeat | None:
        async with self.db_manager.connection_scope() as connection:
            row = await self._fetchrow(
                connection,
                "SELECT * FROM automation_worker_heartbeats WHERE worker_id = ?",
                (worker_id,),
            )
            return row_to_worker_heartbeat(row) if row is not None else None

    async def list_worker_heartbeats(self, *, status: str | None = None) -> list[WorkerHeartbeat]:
        query = "SELECT * FROM automation_worker_heartbeats"
        params: tuple[Any, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY worker_id"
        async with self.db_manager.connection_scope() as connection:
            rows = await self._fetch(connection, query, params)
            return [row_to_worker_heartbeat(row) for row in rows]

    async def get_automation_backlog_summary(self, *, stale_before: datetime) -> AutomationBacklogSummary:
        if stale_before.tzinfo is None or stale_before.utcoffset() is None:
            raise ValueError("automation backlog cutoff must be timezone-aware")
        cutoff = self._db_datetime(stale_before)
        stale_expression = (
            (lambda column: f"{column} < ?")
            if self._postgres
            else (lambda column: f"julianday({column}) < julianday(?)")
        )
        stale_operation_lease = stale_expression("lease_expires_at")
        stale_operation_available = stale_expression("available_at")
        stale_plan_update = stale_expression("updated_at")
        stale_webhook_update = stale_expression("updated_at")
        stale_worker_heartbeat = stale_expression("heartbeat_at")
        latest_heartbeat_expression = (
            "EXTRACT(EPOCH FROM MAX(heartbeat_at))"
            if self._postgres
            else "CAST(strftime('%s', MAX(heartbeat_at)) AS REAL)"
        )

        async with self._serialized_connection() as connection:
            operation_row = await self._fetchrow(
                connection,
                f"""
                SELECT
                    COUNT(CASE WHEN status = 'queued' THEN 1 END) AS queued,
                    COUNT(CASE WHEN status = 'running' THEN 1 END) AS running,
                    COUNT(CASE WHEN status = 'retrying' THEN 1 END) AS retrying,
                    COUNT(CASE WHEN status = 'failed' THEN 1 END) AS failed,
                    COUNT(CASE
                        WHEN status = 'running' AND lease_expires_at IS NOT NULL
                             AND {stale_operation_lease}
                        THEN 1
                        WHEN status IN ('queued', 'retrying') AND {stale_operation_available}
                        THEN 1
                    END) AS stuck
                FROM automation_plan_operations
                """,
                (cutoff, cutoff),
            )
            plan_row = await self._fetchrow(
                connection,
                f"""
                SELECT
                    COUNT(CASE WHEN status = 'applying' THEN 1 END) AS applying,
                    COUNT(CASE WHEN status = 'merged' THEN 1 END) AS merged,
                    COUNT(CASE WHEN status = 'sync_failed' THEN 1 END) AS sync_failed,
                    COUNT(CASE WHEN status IN ('applying', 'merged') AND {stale_plan_update} THEN 1 END) AS stuck
                FROM automation_plans
                """,
                (cutoff,),
            )
            webhook_row = await self._fetchrow(
                connection,
                f"""
                SELECT
                    COUNT(CASE
                        WHEN status IN ('received', 'processing', 'retryable', 'reconcile_unavailable')
                        THEN 1
                    END) AS pending,
                    COUNT(CASE
                        WHEN status IN ('received', 'processing', 'retryable', 'reconcile_unavailable')
                             AND {stale_webhook_update}
                        THEN 1
                    END) AS stuck
                FROM webhook_deliveries
                """,
                (cutoff,),
            )
            worker_row = await self._fetchrow(
                connection,
                f"""
                SELECT
                    COUNT(*) AS total,
                    COUNT(CASE WHEN {stale_worker_heartbeat} THEN 1 END) AS stale,
                    {latest_heartbeat_expression} AS latest_heartbeat
                FROM automation_worker_heartbeats
                """,
                (cutoff,),
            )

        def count(row: Any, key: str) -> int:
            return int(row[key] or 0) if row is not None else 0

        latest_heartbeat = worker_row["latest_heartbeat"] if worker_row is not None else None
        return AutomationBacklogSummary(
            queued_operations=count(operation_row, "queued"),
            running_operations=count(operation_row, "running"),
            retrying_operations=count(operation_row, "retrying"),
            failed_operations=count(operation_row, "failed"),
            stuck_operations=count(operation_row, "stuck"),
            applying_plans=count(plan_row, "applying"),
            merged_plans=count(plan_row, "merged"),
            stuck_plans=count(plan_row, "stuck"),
            sync_failed_plans=count(plan_row, "sync_failed"),
            pending_webhooks=count(webhook_row, "pending"),
            stuck_webhooks=count(webhook_row, "stuck"),
            worker_count=count(worker_row, "total"),
            stale_workers=count(worker_row, "stale"),
            latest_worker_heartbeat_timestamp=float(latest_heartbeat or 0.0),
        )

    async def prune_worker_heartbeats(self, *, cutoff: datetime) -> int:
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("worker heartbeat cutoff must be timezone-aware")
        async with self._transaction() as connection:
            return await self._execute(
                connection,
                "DELETE FROM automation_worker_heartbeats WHERE heartbeat_at < ?",
                (self._db_datetime(cutoff),),
            )

    async def try_acquire_pr_polling_lease(
        self,
        owner_id: str,
        *,
        lease_seconds: int,
    ) -> PullRequestPollingLease | None:
        self._validate_pr_polling_lease_request(owner_id, lease_seconds)
        async with self._polling_lease_transaction() as connection:
            acquired = await self._execute(
                connection,
                f"""
                INSERT INTO gitops_pr_polling_lease
                    (id, owner_id, fencing_token, expires_at, updated_at)
                VALUES (1, ?, 1, {self._lease_expiry_expression}, {self._lease_now_expression})
                ON CONFLICT (id) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    fencing_token = gitops_pr_polling_lease.fencing_token + 1,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                WHERE {self._lease_expired_predicate}
                """,
                (owner_id, lease_seconds),
            )
            if acquired != 1:
                return None
            row = await self._fetchrow(connection, "SELECT * FROM gitops_pr_polling_lease WHERE id = 1")
            if row is None or row["owner_id"] != owner_id:
                raise RuntimeError("acquired PR polling lease could not be read back")
            return self._row_to_pr_polling_lease(row)

    async def renew_pr_polling_lease(
        self,
        lease: PullRequestPollingLease,
        *,
        lease_seconds: int,
    ) -> PullRequestPollingLease:
        if not isinstance(lease, PullRequestPollingLease):
            raise ValueError("lease must be a PullRequestPollingLease")
        self._validate_pr_polling_lease_request(lease.owner_id, lease_seconds)
        async with self._polling_lease_transaction() as connection:
            renewed = await self._execute(
                connection,
                f"""
                UPDATE gitops_pr_polling_lease
                SET expires_at = {self._lease_expiry_expression},
                    updated_at = {self._lease_now_expression}
                WHERE id = 1
                  AND owner_id = ?
                  AND fencing_token = ?
                  AND {self._lease_live_predicate}
                """,
                (lease_seconds, lease.owner_id, lease.fencing_token),
            )
            if renewed != 1:
                raise PullRequestPollingLeaseNotOwnedError("PR polling lease is no longer owned by this worker")
            row = await self._fetchrow(connection, "SELECT * FROM gitops_pr_polling_lease WHERE id = 1")
            if row is None or row["owner_id"] != lease.owner_id or row["fencing_token"] != lease.fencing_token:
                raise RuntimeError("renewed PR polling lease could not be read back")
            return self._row_to_pr_polling_lease(row)

    async def release_pr_polling_lease(self, lease: PullRequestPollingLease) -> bool:
        if not isinstance(lease, PullRequestPollingLease):
            raise ValueError("lease must be a PullRequestPollingLease")
        async with self._polling_lease_transaction() as connection:
            released = await self._execute(
                connection,
                f"""
                UPDATE gitops_pr_polling_lease
                SET expires_at = {self._lease_now_expression},
                    updated_at = {self._lease_now_expression}
                WHERE id = 1
                  AND owner_id = ?
                  AND fencing_token = ?
                  AND {self._lease_live_predicate}
                """,
                (lease.owner_id, lease.fencing_token),
            )
            return released == 1


def _binding_event_identity(value: WorkloadBinding | WorkloadBindingEvent) -> tuple[str, str, str, str]:
    return value.cluster, value.namespace, value.workload_kind, value.workload_name


def _same_binding_material_state(left: WorkloadBinding, right: WorkloadBinding) -> bool:
    left_candidates = sorted(
        (
            candidate.id,
            binding_candidate_generation_token(candidate),
            candidate.confidence,
            candidate.reason,
        )
        for candidate in left.candidates
    )
    right_candidates = sorted(
        (
            candidate.id,
            binding_candidate_generation_token(candidate),
            candidate.confidence,
            candidate.reason,
        )
        for candidate in right.candidates
    )
    left_source_id = left.source.id if left.source is not None else None
    right_source_id = right.source.id if right.source is not None else None
    return (
        left.status == right.status
        and left.confidence == right.confidence
        and (left.source is not None) == (right.source is not None)
        and left_source_id == right_source_id
        and left.reason == right.reason
        and left.evidence == right.evidence
        and left.candidates_truncated == right.candidates_truncated
        and left_candidates == right_candidates
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
