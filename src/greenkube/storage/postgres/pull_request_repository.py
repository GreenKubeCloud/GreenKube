# src/greenkube/storage/postgres/pull_request_repository.py
"""PostgreSQL implementation of the pull-request tracking repository."""

import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from greenkube.models.metrics import PullRequestRecord, PullRequestStatus
from greenkube.storage.base_pull_request_repository import (
    BRANCH_CLEANUP_PLAN_STATUSES,
    ORPHANED_PENDING_PULL_REQUEST_ERROR,
    PullRequestRepository,
    PullRequestStateConflictError,
)
from greenkube.storage.pull_request_mapper import MUTABLE_PR_COLUMNS, row_to_pull_request

logger = logging.getLogger(__name__)


def _encode(value):
    if isinstance(value, Enum):
        return value.value
    return value


class PostgresPullRequestRepository(PullRequestRepository):
    """Persists pull-request attempts in PostgreSQL."""

    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def save_pull_request(self, record: PullRequestRecord) -> PullRequestRecord:
        created = record.created_at or datetime.now(timezone.utc)
        status_value = record.status.value if isinstance(record.status, PullRequestStatus) else record.status
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO recommendation_pull_requests
                    (recommendation_id, provider, repo, base_branch, head_branch,
                     pr_number, pr_url, status, error, plan_id, provider_id, commit_sha,
                     merge_commit_sha, merged_at, closed_at, sync_status, sync_revision,
                     sync_observed_at, sync_error_code, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                        $15, $16, $17, $18, $19, $20, $21)
                RETURNING *
                """,
                record.recommendation_id,
                record.provider,
                record.repo,
                record.base_branch,
                record.head_branch,
                record.pr_number,
                record.pr_url,
                status_value,
                record.error,
                record.plan_id,
                record.provider_id,
                record.commit_sha,
                record.merge_commit_sha,
                record.merged_at,
                record.closed_at,
                record.sync_status,
                record.sync_revision,
                record.sync_observed_at,
                record.sync_error_code,
                created,
                record.updated_at or created,
            )
            return row_to_pull_request(row)

    async def update_pull_request(
        self,
        pr_id: int,
        updates: dict,
        *,
        expected_statuses: Sequence[str] | None = None,
        expected_plan_id: str | None = None,
    ) -> PullRequestRecord:
        filtered = {k: v for k, v in updates.items() if k in MUTABLE_PR_COLUMNS}
        if not filtered:
            raise ValueError("No mutable pull-request columns supplied.")
        filtered.setdefault("updated_at", datetime.now(timezone.utc))

        set_clause = ", ".join(f"{column} = ${index}" for index, column in enumerate(filtered, start=1))
        params = [_encode(value) for value in filtered.values()]
        conditions = [f"id = ${len(params) + 1}"]
        condition_params: list[object] = [pr_id]
        next_index = len(params) + 2
        if expected_statuses is not None:
            statuses = tuple(dict.fromkeys(expected_statuses))
            if not statuses:
                raise ValueError("expected_statuses must not be empty")
            placeholders = ", ".join(f"${next_index + offset}" for offset in range(len(statuses)))
            conditions.append(f"status IN ({placeholders})")
            condition_params.extend(statuses)
            next_index += len(statuses)
        if expected_plan_id is not None:
            conditions.append(f"plan_id = ${next_index}")
            condition_params.append(expected_plan_id)

        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(
                f"UPDATE recommendation_pull_requests SET {set_clause} WHERE {' AND '.join(conditions)} RETURNING *",
                *params,
                *condition_params,
            )
            if not row:
                exists = await conn.fetchval("SELECT 1 FROM recommendation_pull_requests WHERE id = $1", pr_id)
                if not exists:
                    raise ValueError(f"Pull request {pr_id} not found.")
                raise PullRequestStateConflictError(f"Pull request {pr_id} changed before the update was applied.")
            return row_to_pull_request(row)

    async def get_pull_request_by_id(self, pr_id: int) -> Optional[PullRequestRecord]:
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow("SELECT * FROM recommendation_pull_requests WHERE id = $1", pr_id)
            return row_to_pull_request(row) if row else None

    async def get_pull_request_by_provider_reference(
        self, provider_id: str, repo: str, pr_number: int
    ) -> Optional[PullRequestRecord]:
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM recommendation_pull_requests "
                "WHERE provider_id = $1 AND repo = $2 AND pr_number = $3 "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                provider_id,
                repo,
                pr_number,
            )
            return row_to_pull_request(row) if row else None

    async def list_pull_requests_by_provider_reference(
        self,
        provider_id: str,
        repo: str,
        pr_number: int,
    ) -> List[PullRequestRecord]:
        async with self.db_manager.connection_scope() as conn:
            rows = await conn.fetch(
                "SELECT * FROM recommendation_pull_requests "
                "WHERE provider_id = $1 AND repo = $2 AND pr_number = $3 "
                "ORDER BY created_at DESC, id DESC",
                provider_id,
                repo,
                pr_number,
            )
            return [row_to_pull_request(row) for row in rows]

    async def get_pull_request_by_plan_id(self, plan_id: str) -> Optional[PullRequestRecord]:
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM recommendation_pull_requests WHERE plan_id = $1 "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                plan_id,
            )
            return row_to_pull_request(row) if row else None

    async def list_pull_requests_by_plan_id(self, plan_id: str) -> List[PullRequestRecord]:
        async with self.db_manager.connection_scope() as conn:
            rows = await conn.fetch(
                "SELECT * FROM recommendation_pull_requests WHERE plan_id = $1 ORDER BY id ASC",
                plan_id,
            )
            return [row_to_pull_request(row) for row in rows]

    async def get_pull_requests_for_recommendation(self, recommendation_id: int) -> List[PullRequestRecord]:
        async with self.db_manager.connection_scope() as conn:
            rows = await conn.fetch(
                "SELECT * FROM recommendation_pull_requests WHERE recommendation_id = $1 "
                "ORDER BY created_at DESC, id DESC",
                recommendation_id,
            )
            return [row_to_pull_request(r) for r in rows]

    async def get_open_pull_requests(self, recommendation_id: Optional[int] = None) -> List[PullRequestRecord]:
        params: list = []
        query = "SELECT * FROM recommendation_pull_requests WHERE status IN ('pending', 'open')"
        if recommendation_id is not None:
            params.append(recommendation_id)
            query += f" AND recommendation_id = ${len(params)}"
        query += " ORDER BY created_at DESC"

        async with self.db_manager.connection_scope() as conn:
            rows = await conn.fetch(query, *params)
            return [row_to_pull_request(r) for r in rows]

    async def list_pollable_pull_requests(
        self,
        *,
        limit: int,
        after_id: int | None = None,
    ) -> List[PullRequestRecord]:
        self.validate_poll_page(limit, after_id)
        query = (
            "SELECT * FROM recommendation_pull_requests "
            "WHERE status IN ('pending', 'open') "
            "AND plan_id IS NOT NULL AND plan_id <> '' "
            "AND provider_id IS NOT NULL AND provider_id <> '' "
            "AND repo <> '' AND pr_number IS NOT NULL AND pr_number > 0"
        )
        params: list = []
        if after_id is not None:
            params.append(after_id)
            query += f" AND id > ${len(params)}"
        params.append(limit)
        query += f" ORDER BY id ASC LIMIT ${len(params)}"

        async with self.db_manager.connection_scope() as conn:
            rows = await conn.fetch(query, *params)
            return [row_to_pull_request(row) for row in rows]

    async def list_sync_pending_pull_requests(
        self,
        *,
        limit: int,
        after_id: int | None = None,
    ) -> List[PullRequestRecord]:
        self.validate_poll_page(limit, after_id)
        query = (
            "SELECT * FROM recommendation_pull_requests "
            "WHERE status = 'merged' "
            "AND plan_id IS NOT NULL AND plan_id <> '' "
            "AND provider_id IS NOT NULL AND provider_id <> '' "
            "AND repo <> '' AND pr_number IS NOT NULL AND pr_number > 0 "
            "AND merge_commit_sha IS NOT NULL AND merge_commit_sha <> '' "
            "AND (sync_status IS NULL OR sync_status NOT IN ('synced', 'failed') "
            "OR (sync_status = 'failed' AND sync_error_code = 'sync_timeout'))"
        )
        params: list = []
        if after_id is not None:
            params.append(after_id)
            query += f" AND id > ${len(params)}"
        params.append(limit)
        query += f" ORDER BY id ASC LIMIT ${len(params)}"

        async with self.db_manager.connection_scope() as conn:
            rows = await conn.fetch(query, *params)
            return [row_to_pull_request(row) for row in rows]

    async def list_branch_cleanup_candidates(
        self,
        *,
        limit: int,
        after_id: int | None = None,
    ) -> List[PullRequestRecord]:
        self.validate_cleanup_page(limit, after_id)
        status_placeholders = ", ".join(f"${index}" for index in range(1, len(BRANCH_CLEANUP_PLAN_STATUSES) + 1))
        query = (
            "SELECT pr.* FROM recommendation_pull_requests pr "
            "JOIN automation_plans plan ON plan.plan_id = pr.plan_id "
            "LEFT JOIN automation_branch_cleanup_completed cleanup "
            "ON cleanup.plan_id = pr.plan_id AND cleanup.provider_id = pr.provider_id "
            "AND cleanup.repo = pr.repo AND cleanup.branch_name = pr.head_branch "
            "WHERE cleanup.plan_id IS NULL "
            "AND pr.plan_id IS NOT NULL AND pr.plan_id <> '' "
            "AND pr.head_branch IS NOT NULL AND pr.head_branch <> '' "
            f"AND (pr.status IN ('closed', 'merged') OR plan.status IN ({status_placeholders}))"
        )
        params: list[object] = list(BRANCH_CLEANUP_PLAN_STATUSES)
        if after_id is not None:
            params.append(after_id)
            query += f" AND pr.id > ${len(params)}"
        params.append(limit)
        query += f" ORDER BY pr.id ASC LIMIT ${len(params)}"
        async with self.db_manager.connection_scope() as conn:
            rows = await conn.fetch(query, *params)
            return [row_to_pull_request(row) for row in rows]

    async def mark_plan_branch_cleanup_complete(
        self,
        plan_id: str,
        *,
        provider_id: str,
        repo: str,
        branch_name: str,
    ) -> None:
        async with self.db_manager.connection_scope() as conn:
            await conn.execute(
                """
                INSERT INTO automation_branch_cleanup_completed
                    (plan_id, provider_id, repo, branch_name, completed_at)
                VALUES ($1, $2, $3, $4, NOW())
                ON CONFLICT (plan_id) DO UPDATE SET
                    provider_id = EXCLUDED.provider_id,
                    repo = EXCLUDED.repo,
                    branch_name = EXCLUDED.branch_name,
                    completed_at = EXCLUDED.completed_at
                """,
                plan_id,
                provider_id,
                repo,
                branch_name,
            )

    async def reconcile_open_pull_requests(self, *, stale_before: datetime, limit: int) -> int:
        if not isinstance(stale_before, datetime) or stale_before.tzinfo is None or stale_before.utcoffset() is None:
            raise ValueError("stale_before must be a timezone-aware datetime.")
        self.validate_poll_page(limit, None)
        async with self.db_manager.connection_scope() as conn:
            result = await conn.execute(
                """
                UPDATE recommendation_pull_requests
                SET status = 'error',
                    error = COALESCE(error, $1),
                    updated_at = NOW()
                WHERE id IN (
                    SELECT id
                    FROM recommendation_pull_requests
                    WHERE status = 'pending'
                      AND (plan_id IS NULL OR BTRIM(plan_id) = '')
                      AND (provider_id IS NULL OR BTRIM(provider_id) = '')
                      AND pr_number IS NULL
                      AND (pr_url IS NULL OR BTRIM(pr_url) = '')
                      AND created_at <= $2
                    ORDER BY id ASC
                    LIMIT $3
                )
                """,
                ORPHANED_PENDING_PULL_REQUEST_ERROR,
                stale_before,
                limit,
            )
            return int(result.split()[-1])
