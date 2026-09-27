# src/greenkube/storage/sqlite/pull_request_repository.py
"""SQLite implementation of the pull-request tracking repository."""

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

import aiosqlite

from greenkube.models.metrics import PullRequestRecord, PullRequestStatus
from greenkube.storage.base_pull_request_repository import PullRequestRepository
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
                     pr_number, pr_url, status, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    created,
                    to_iso_z(record.updated_at) if record.updated_at else created,
                ),
            )
            await conn.commit()
            cursor = await conn.execute("SELECT * FROM recommendation_pull_requests WHERE id = ?", (cursor.lastrowid,))
            row = await cursor.fetchone()
            return row_to_pull_request(row)

    async def update_pull_request(self, pr_id: int, updates: dict) -> PullRequestRecord:
        filtered = {k: v for k, v in updates.items() if k in MUTABLE_PR_COLUMNS}
        if not filtered:
            raise ValueError("No mutable pull-request columns supplied.")
        filtered.setdefault("updated_at", datetime.now(timezone.utc))

        set_clause = ", ".join(f"{column} = ?" for column in filtered)
        params = [_encode(value) for value in filtered.values()]
        params.append(pr_id)

        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT id FROM recommendation_pull_requests WHERE id = ?", (pr_id,))
            if not await cursor.fetchone():
                raise ValueError(f"Pull request {pr_id} not found.")
            await conn.execute(f"UPDATE recommendation_pull_requests SET {set_clause} WHERE id = ?", params)
            await conn.commit()
            cursor = await conn.execute("SELECT * FROM recommendation_pull_requests WHERE id = ?", (pr_id,))
            return row_to_pull_request(await cursor.fetchone())

    async def get_pull_request_by_id(self, pr_id: int) -> Optional[PullRequestRecord]:
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM recommendation_pull_requests WHERE id = ?", (pr_id,))
            row = await cursor.fetchone()
            return row_to_pull_request(row) if row else None

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
