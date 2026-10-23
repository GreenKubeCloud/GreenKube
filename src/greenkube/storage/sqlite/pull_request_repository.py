# src/greenkube/storage/sqlite/pull_request_repository.py
"""SQLite implementation of the pull-request tracking repository."""

import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

import aiosqlite

from greenkube.models.metrics import PullRequestRecord, PullRequestStatus
from greenkube.storage.base_pull_request_repository import (
    BRANCH_CLEANUP_PLAN_STATUSES,
    ORPHANED_PENDING_PULL_REQUEST_ERROR,
    PullRequestRepository,
    PullRequestStateConflictError,
)
from greenkube.storage.pull_request_mapper import MUTABLE_PR_COLUMNS, row_to_pull_request
from greenkube.utils.date_utils import to_iso_z

logger = logging.getLogger(__name__)


def _encode(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return to_iso_z(value)
    return value


class SQLitePullRequestRepository(PullRequestRepository):
    """Persists pull-request attempts in SQLite."""

    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def save_pull_request(self, record: PullRequestRecord) -> PullRequestRecord:
        created = to_iso_z(record.created_at or datetime.now(timezone.utc))
        async with self.db_manager.connection_scope() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO recommendation_pull_requests
                    (recommendation_id, provider, repo, base_branch, head_branch,
                     pr_number, pr_url, status, error, plan_id, provider_id, commit_sha,
                     merge_commit_sha, merged_at, closed_at, sync_status, sync_revision,
                     sync_observed_at, sync_error_code, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.recommendation_id,
                    record.provider,
                    record.repo,
                    record.base_branch,
                    record.head_branch,
                    record.pr_number,
                    record.pr_url,
                    record.status.value if isinstance(record.status, PullRequestStatus) else record.status,
                    record.error,
                    record.plan_id,
                    record.provider_id,
                    record.commit_sha,
                    record.merge_commit_sha,
                    _encode(record.merged_at),
                    _encode(record.closed_at),
                    record.sync_status,
                    record.sync_revision,
                    _encode(record.sync_observed_at),
                    record.sync_error_code,
                    created,
                    to_iso_z(record.updated_at) if record.updated_at else created,
                ),
            )
            await conn.commit()
            cursor = await conn.execute("SELECT * FROM recommendation_pull_requests WHERE id = ?", (cursor.lastrowid,))
            row = await cursor.fetchone()
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

        set_clause = ", ".join(f"{column} = ?" for column in filtered)
        params = [_encode(value) for value in filtered.values()]
        conditions = ["id = ?"]
        condition_params: list[object] = [pr_id]
        if expected_statuses is not None:
            statuses = tuple(dict.fromkeys(expected_statuses))
            if not statuses:
                raise ValueError("expected_statuses must not be empty")
            conditions.append(f"status IN ({', '.join('?' for _ in statuses)})")
            condition_params.extend(statuses)
        if expected_plan_id is not None:
            conditions.append("plan_id = ?")
            condition_params.append(expected_plan_id)

        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                f"UPDATE recommendation_pull_requests SET {set_clause} WHERE {' AND '.join(conditions)}",
                (*params, *condition_params),
            )
            if cursor.rowcount == 0:
                cursor = await conn.execute("SELECT id FROM recommendation_pull_requests WHERE id = ?", (pr_id,))
                if not await cursor.fetchone():
                    raise ValueError(f"Pull request {pr_id} not found.")
                raise PullRequestStateConflictError(f"Pull request {pr_id} changed before the update was applied.")
            await conn.commit()
            cursor = await conn.execute("SELECT * FROM recommendation_pull_requests WHERE id = ?", (pr_id,))
            return row_to_pull_request(await cursor.fetchone())

    async def get_pull_request_by_id(self, pr_id: int) -> Optional[PullRequestRecord]:
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM recommendation_pull_requests WHERE id = ?", (pr_id,))
            row = await cursor.fetchone()
            return row_to_pull_request(row) if row else None

    async def get_pull_request_by_provider_reference(
        self, provider_id: str, repo: str, pr_number: int
    ) -> Optional[PullRequestRecord]:
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM recommendation_pull_requests "
                "WHERE provider_id = ? AND repo = ? AND pr_number = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (provider_id, repo, pr_number),
            )
            row = await cursor.fetchone()
            return row_to_pull_request(row) if row else None

    async def list_pull_requests_by_provider_reference(
        self,
        provider_id: str,
        repo: str,
        pr_number: int,
    ) -> List[PullRequestRecord]:
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM recommendation_pull_requests "
                "WHERE provider_id = ? AND repo = ? AND pr_number = ? "
                "ORDER BY created_at DESC, id DESC",
                (provider_id, repo, pr_number),
            )
            rows = await cursor.fetchall()
            return [row_to_pull_request(row) for row in rows]

    async def get_pull_request_by_plan_id(self, plan_id: str) -> Optional[PullRequestRecord]:
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM recommendation_pull_requests WHERE plan_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (plan_id,),
            )
            row = await cursor.fetchone()
            return row_to_pull_request(row) if row else None

    async def list_pull_requests_by_plan_id(self, plan_id: str) -> List[PullRequestRecord]:
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM recommendation_pull_requests WHERE plan_id = ? ORDER BY id ASC",
                (plan_id,),
            )
            return [row_to_pull_request(row) for row in await cursor.fetchall()]

    async def get_pull_requests_for_recommendation(self, recommendation_id: int) -> List[PullRequestRecord]:
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM recommendation_pull_requests "
                "WHERE recommendation_id = ? ORDER BY created_at DESC, id DESC",
                (recommendation_id,),
            )
            rows = await cursor.fetchall()
            return [row_to_pull_request(r) for r in rows]

    async def get_open_pull_requests(self, recommendation_id: Optional[int] = None) -> List[PullRequestRecord]:
        query = "SELECT * FROM recommendation_pull_requests WHERE status IN ('pending', 'open')"
        params: list = []
        if recommendation_id is not None:
            query += " AND recommendation_id = ?"
            params.append(recommendation_id)
        query += " ORDER BY created_at DESC"

        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
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
            query += " AND id > ?"
            params.append(after_id)
        query += " ORDER BY id ASC LIMIT ?"
        params.append(limit)

        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
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
            query += " AND id > ?"
            params.append(after_id)
        query += " ORDER BY id ASC LIMIT ?"
        params.append(limit)

        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return [row_to_pull_request(row) for row in rows]

    async def list_branch_cleanup_candidates(
        self,
        *,
        limit: int,
        after_id: int | None = None,
    ) -> List[PullRequestRecord]:
        self.validate_cleanup_page(limit, after_id)
        placeholders = ", ".join("?" for _ in BRANCH_CLEANUP_PLAN_STATUSES)
        query = (
            "SELECT pr.* FROM recommendation_pull_requests pr "
            "JOIN automation_plans plan ON plan.plan_id = pr.plan_id "
            "LEFT JOIN automation_branch_cleanup_completed cleanup "
            "ON cleanup.plan_id = pr.plan_id AND cleanup.provider_id = pr.provider_id "
            "AND cleanup.repo = pr.repo AND cleanup.branch_name = pr.head_branch "
            "WHERE cleanup.plan_id IS NULL "
            "AND pr.plan_id IS NOT NULL AND pr.plan_id <> '' "
            "AND pr.head_branch IS NOT NULL AND pr.head_branch <> '' "
            f"AND (pr.status IN ('closed', 'merged') OR plan.status IN ({placeholders}))"
        )
        params: list = list(BRANCH_CLEANUP_PLAN_STATUSES)
        if after_id is not None:
            query += " AND pr.id > ?"
            params.append(after_id)
        query += " ORDER BY pr.id ASC LIMIT ?"
        params.append(limit)
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(query, params)
            return [row_to_pull_request(row) for row in await cursor.fetchall()]

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
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(plan_id) DO UPDATE SET
                    provider_id = excluded.provider_id,
                    repo = excluded.repo,
                    branch_name = excluded.branch_name,
                    completed_at = excluded.completed_at
                """,
                (plan_id, provider_id, repo, branch_name),
            )
            await conn.commit()

    async def reconcile_open_pull_requests(self, *, stale_before: datetime, limit: int) -> int:
        if not isinstance(stale_before, datetime) or stale_before.tzinfo is None or stale_before.utcoffset() is None:
            raise ValueError("stale_before must be a timezone-aware datetime.")
        self.validate_poll_page(limit, None)
        async with self.db_manager.connection_scope() as conn:
            cursor = await conn.execute(
                """
                UPDATE recommendation_pull_requests
                SET status = 'error',
                    error = COALESCE(error, ?),
                    updated_at = ?
                WHERE id IN (
                    SELECT id
                    FROM recommendation_pull_requests
                    WHERE status = 'pending'
                      AND (plan_id IS NULL OR TRIM(plan_id) = '')
                      AND (provider_id IS NULL OR TRIM(provider_id) = '')
                      AND pr_number IS NULL
                      AND (pr_url IS NULL OR TRIM(pr_url) = '')
                      AND datetime(created_at) <= datetime(?)
                    ORDER BY id ASC
                    LIMIT ?
                )
                """,
                (
                    ORPHANED_PENDING_PULL_REQUEST_ERROR,
                    to_iso_z(datetime.now(timezone.utc)),
                    to_iso_z(stale_before),
                    limit,
                ),
            )
            await conn.commit()
            return cursor.rowcount
