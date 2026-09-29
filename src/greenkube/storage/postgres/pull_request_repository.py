# src/greenkube/storage/postgres/pull_request_repository.py
"""PostgreSQL implementation of the pull-request tracking repository."""

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from greenkube.models.metrics import PullRequestRecord, PullRequestStatus
from greenkube.storage.base_pull_request_repository import PullRequestRepository
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
                     pr_number, pr_url, status, error, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
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
                created,
                record.updated_at or created,
            )
            return row_to_pull_request(row)

    async def update_pull_request(self, pr_id: int, updates: dict) -> PullRequestRecord:
        filtered = {k: v for k, v in updates.items() if k in MUTABLE_PR_COLUMNS}
        if not filtered:
            raise ValueError("No mutable pull-request columns supplied.")
        filtered.setdefault("updated_at", datetime.now(timezone.utc))

        set_clause = ", ".join(f"{column} = ${index}" for index, column in enumerate(filtered, start=1))
        params = [_encode(value) for value in filtered.values()]
        params.append(pr_id)

        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(
                f"UPDATE recommendation_pull_requests SET {set_clause} WHERE id = ${len(params)} RETURNING *",
                *params,
            )
            if not row:
                raise ValueError(f"Pull request {pr_id} not found.")
            return row_to_pull_request(row)

    async def get_pull_request_by_id(self, pr_id: int) -> Optional[PullRequestRecord]:
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow("SELECT * FROM recommendation_pull_requests WHERE id = $1", pr_id)
            return row_to_pull_request(row) if row else None

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

    async def reconcile_open_pull_requests(self) -> int:
        async with self.db_manager.connection_scope() as conn:
            result = await conn.execute(
                "UPDATE recommendation_pull_requests SET status = 'error', "
                "error = COALESCE(error, 'operation recovery required'), updated_at = NOW() WHERE status = 'pending'"
            )
            return int(result.split()[-1])
