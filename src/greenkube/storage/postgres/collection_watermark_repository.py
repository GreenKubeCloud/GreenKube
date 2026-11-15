"""PostgreSQL collection watermark repository."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from greenkube.storage.base_collection_watermark_repository import CollectionWatermark, CollectionWatermarkRepository
from greenkube.utils.time import ensure_utc


def _row(row) -> CollectionWatermark:
    return CollectionWatermark(
        cluster_uid=row["cluster_uid"],
        source=row["source"],
        last_complete_end=ensure_utc(row["last_complete_end"]),
        updated_at=ensure_utc(row["updated_at"]),
    )


class PostgresCollectionWatermarkRepository(CollectionWatermarkRepository):
    def __init__(self, db_manager) -> None:
        self.db_manager = db_manager

    async def get(self, cluster_uid: str, source: str) -> CollectionWatermark | None:
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(
                """SELECT cluster_uid, source, last_complete_end, updated_at
                FROM collection_watermarks WHERE cluster_uid = $1 AND source = $2""",
                cluster_uid,
                source,
            )
        return _row(row) if row else None

    async def advance(
        self, cluster_uid: str, source: str, last_complete_end: datetime, *, connection: Any = None
    ) -> CollectionWatermark:
        """See :meth:`CollectionWatermarkRepository.advance`."""
        if connection is not None:
            return await self._advance(connection, cluster_uid, source, last_complete_end)
        async with self.db_manager.connection_scope() as conn:
            return await self._advance(conn, cluster_uid, source, last_complete_end)

    @staticmethod
    async def _advance(conn: Any, cluster_uid: str, source: str, last_complete_end: datetime) -> CollectionWatermark:
        row = await conn.fetchrow(
            """INSERT INTO collection_watermarks (cluster_uid, source, last_complete_end, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (cluster_uid, source) DO UPDATE SET
                last_complete_end = GREATEST(collection_watermarks.last_complete_end, EXCLUDED.last_complete_end),
                updated_at = NOW()
            RETURNING cluster_uid, source, last_complete_end, updated_at""",
            cluster_uid,
            source,
            ensure_utc(last_complete_end),
        )
        return _row(row)

    async def list_all(self, cluster_uid: str | None = None) -> list[CollectionWatermark]:
        async with self.db_manager.connection_scope() as conn:
            if cluster_uid is None:
                rows = await conn.fetch(
                    """SELECT cluster_uid, source, last_complete_end, updated_at
                    FROM collection_watermarks ORDER BY cluster_uid, source"""
                )
            else:
                rows = await conn.fetch(
                    """SELECT cluster_uid, source, last_complete_end, updated_at
                    FROM collection_watermarks WHERE cluster_uid = $1 ORDER BY source""",
                    cluster_uid,
                )
        return [_row(row) for row in rows]
